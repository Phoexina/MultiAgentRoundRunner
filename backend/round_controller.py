"""
Round Controller

Round N-M: N = round number (1-based), M = step number (1-based)
Round 0  : initial state — no sessions exist

Core rule: only opening a new session changes the Round.
           Waiting between steps does NOT change Round state.
"""

import asyncio
import json
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Awaitable
from dataclasses import dataclass, field

import logging
log = logging.getLogger(__name__)

MAX_ROUNDS = 0  # 0 = unlimited

DEFAULT_PROMPT_TEMPLATE = """\
## 对话历史
{chat_history}

---

## 本轮指令
- 当前轮次：{round_number}

## 用户意见（本轮开始前用户输入，无则忽略）
{user_input}

## 上一步骤总结
{last_summary}
"""


# ---------------------------------------------------------------------------
# Helpers for extracting inline text from raw NDJSON event strings
# ---------------------------------------------------------------------------

def _extract_claude_text_delta(event_json: str) -> str:
    try:
        e = json.loads(event_json)
        if (isinstance(e, dict)
                and e.get('type') == 'stream_event'
                and isinstance(e.get('event'), dict)
                and e['event'].get('type') == 'content_block_delta'):
            d = e['event'].get('delta') or {}
            if d.get('type') == 'text_delta':
                return d.get('text') or ''
    except Exception:
        pass
    return ''


def _extract_codex_text(event_json: str) -> str:
    try:
        e = json.loads(event_json)
        item = e.get('item') if isinstance(e, dict) else None
        if not isinstance(item, dict):
            return ''
        if item.get('type') == 'agent_message':
            return item.get('text') or ''
        if item.get('type') == 'message':
            parts = [
                p.get('text', '')
                for p in (item.get('content') or [])
                if isinstance(p, dict) and p.get('type') == 'output_text'
            ]
            return '\n'.join(p for p in parts if p)
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class RoundState(Enum):
    IDLE    = "idle"
    RUNNING = "running"


@dataclass
class RoundData:
    """All state for the current (or most recent) round."""
    round_index:     int            = 0       # N in Round N-M (0 = initial state)
    step_index:      int            = 1       # M in Round N-M (1-based)
    conversation_id: Optional[int]  = None
    pipeline_steps:  list           = field(default_factory=list)
    session_pk:      Optional[int]  = None    # DB sessions.id (PK of current row)
    working_dir:     Optional[str]  = None
    resume_id:       Optional[str]  = None    # cli_session_id for explicit --resume
    user_inputs:     list           = field(default_factory=list)  # intervention messages


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class RoundController:
    """
    Orchestrates pipeline steps, tracks Round N-M state, manages agent processes.
    """

    def __init__(
        self,
        db,
        process_manager,
        broadcast_callback: Callable[[dict], Awaitable[None]],
    ):
        self.db              = db
        self.process_manager = process_manager
        self.broadcast       = broadcast_callback

        self.state         = RoundState.IDLE
        self.current_round = RoundData()

        self._stop_requested     = False
        self._stop_after_current = False
        self._lock               = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public read-only helpers
    # ------------------------------------------------------------------

    def get_state(self) -> RoundState:
        return self.state

    def get_current_round(self) -> RoundData:
        return self.current_round

    def is_running(self) -> bool:
        return self.state == RoundState.RUNNING

    # ------------------------------------------------------------------
    # Control commands
    # ------------------------------------------------------------------

    async def start(self, conversation_id: Optional[int] = None) -> None:
        """Begin automation from Round 1-1."""
        async with self._lock:
            if self.state == RoundState.RUNNING:
                log.warning("Cannot start: already running")
                return

            self._stop_requested     = False
            self._stop_after_current = False

            steps = []
            working_dir = None
            if conversation_id:
                steps = await self.db.get_pipeline_steps(conversation_id)
                conv  = await self.db.get_conversation(conversation_id)
                if conv:
                    working_dir = conv.get('working_dir')

            if not steps:
                steps = [
                    {'agent_type': 'codex',  'prompt_template': None},
                    {'agent_type': 'claude', 'prompt_template': None},
                ]

            self.current_round = RoundData(
                round_index     = 1,
                step_index      = 1,
                conversation_id = conversation_id,
                pipeline_steps  = steps,
                working_dir     = working_dir,
            )

            self.state = RoundState.RUNNING
            await self._broadcast_status()
            asyncio.create_task(self.run_loop())

    async def stop(self) -> None:
        """Kill the current session immediately. Round display stays at N-M."""
        self._stop_requested = True
        async with self._lock:
            if self.state != RoundState.RUNNING:
                return
            await self.process_manager.stop_current()
            # chat column stays NULL — UI will read JSONL file for last paragraph
            self.state = RoundState.IDLE
            await self._broadcast_status()

    async def stop_after_current(self) -> None:
        """Let the current session finish, then stop (don't continue next step)."""
        if self.state != RoundState.RUNNING:
            return
        self._stop_after_current = True
        await self.broadcast({'type': 'system', 'content': '当前 session 完成后将停止'})

    async def continue_next(self, conversation_id: Optional[int] = None) -> bool:
        """Advance to the next step and resume the loop.

        If no in-memory state (e.g. after server restart), restores from DB.
        Returns True if accepted.
        """
        async with self._lock:
            if self.state == RoundState.RUNNING:
                log.warning("Cannot continue: already running")
                return False

            self._stop_requested     = False
            self._stop_after_current = False

            if self.current_round.round_index == 0:
                # No in-memory state — restore from DB
                conv_id = conversation_id or self.current_round.conversation_id
                if not conv_id or not await self._restore_from_db(conv_id, for_retry=False):
                    await self.broadcast({'type': 'system', 'content': '没有可继续的流程'})
                    return False
            else:
                # Advance step in memory
                next_step = self.current_round.step_index + 1
                if next_step > len(self.current_round.pipeline_steps):
                    # All steps done — start next round
                    max_rounds = MAX_ROUNDS
                    if max_rounds > 0 and self.current_round.round_index >= max_rounds:
                        await self.broadcast({
                            'type': 'system',
                            'content': f'已达最大轮次 {max_rounds}，自动停止',
                        })
                        return False
                    self.current_round.round_index += 1
                    self.current_round.step_index   = 1
                else:
                    self.current_round.step_index = next_step
                self.current_round.resume_id  = None
                self.current_round.session_pk = None

            self.state = RoundState.RUNNING
            await self._broadcast_status()
            asyncio.create_task(self.run_loop())
            return True

    async def retry(self, conversation_id: Optional[int] = None) -> bool:
        """Re-run the current step without advancing. Creates a fresh session row.

        Returns True if accepted.
        """
        async with self._lock:
            if self.state == RoundState.RUNNING:
                log.warning("Cannot retry: already running")
                return False

            self._stop_requested     = False
            self._stop_after_current = False

            if self.current_round.round_index == 0:
                conv_id = conversation_id or self.current_round.conversation_id
                if not conv_id or not await self._restore_from_db(conv_id, for_retry=True):
                    await self.broadcast({'type': 'system', 'content': '没有可重试的步骤'})
                    return False

            # Clear resume_id and session_pk so _run_step creates a fresh row
            self.current_round.resume_id  = None
            self.current_round.session_pk = None

            self.state = RoundState.RUNNING
            await self._broadcast_status()
            asyncio.create_task(self.run_loop())
            return True

    async def resume_session(self, session_id: int, user_input: Optional[str] = None) -> bool:
        """Resume a historical session using its own cli_session_id (--resume).

        Works for both Claude (session_id) and Codex (thread_id).
        Reuses the original DB session record.
        Returns True if accepted.
        """
        async with self._lock:
            if self.state == RoundState.RUNNING:
                await self.broadcast({'type': 'system', 'content': '无法继续：代理正在运行中'})
                return False

            session = await self.db.get_session(session_id)
            if not session:
                await self.broadcast({'type': 'system', 'content': '找不到该会话记录'})
                return False

            conversation_id = session['conv_id']
            steps = await self.db.get_pipeline_steps(conversation_id) or [
                {'agent_type': 'codex',  'prompt_template': None},
                {'agent_type': 'claude', 'prompt_template': None},
            ]
            conv        = await self.db.get_conversation(conversation_id)
            working_dir = conv.get('working_dir') if conv else None

            cli_id = session.get('session_id')   # the stored cli_session_id

            self._stop_requested     = False
            self._stop_after_current = False

            self.current_round = RoundData(
                round_index     = session['round_index'],
                step_index      = session['step_index'],
                conversation_id = conversation_id,
                pipeline_steps  = steps,
                session_pk      = session_id,    # reuse original record
                working_dir     = working_dir,
                resume_id       = cli_id,         # --resume
                user_inputs     = [user_input.strip()] if user_input and user_input.strip() else [],
            )

            log.info(
                f"Resume session pk={session_id}: Round {session['round_index']}-"
                f"{session['step_index']} ({session['agent_type']}), resume_id={cli_id!r}"
            )

            self.state = RoundState.RUNNING
            await self._broadcast_status()
            asyncio.create_task(self.run_loop())
            return True

    async def receive_user_message(self, content: str) -> None:
        """Accept a user message during the intervention window."""
        self.current_round.user_inputs.append(content)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Run one pipeline step, then open an intervention window.

        For automated multi-step runs (no intervention timeout) this loops
        through all steps without pausing.
        """
        try:
            while True:
                if self._stop_requested:
                    break

                step = self._current_step()
                if step is None:
                    break

                api_error = await self._run_step(step)

                if self._stop_requested or self._stop_after_current:
                    break

                if api_error:
                    await self.broadcast({'type': 'system', 'content': 'API 错误，等待 60 秒后继续会话...'})
                    await asyncio.sleep(60)
                    if self._stop_requested:
                        break
                    await self._run_step(step)
                    if self._stop_requested or self._stop_after_current:
                        break

                # Advance to next step
                next_step = self.current_round.step_index + 1
                if next_step > len(self.current_round.pipeline_steps):
                    # All steps done — check max_rounds, then start next round
                    max_rounds = MAX_ROUNDS
                    if max_rounds > 0 and self.current_round.round_index >= max_rounds:
                        await self.broadcast({
                            'type': 'system',
                            'content': f'已达最大轮次 {max_rounds}，自动停止',
                        })
                        break
                    self.current_round.round_index += 1
                    self.current_round.step_index   = 1
                else:
                    self.current_round.step_index = next_step

                self.current_round.resume_id  = None
                self.current_round.session_pk = None

        except Exception as e:
            log.exception(f"Error in run_loop: {e}")
            await self.broadcast({'type': 'system', 'content': f'错误: {e}'})

        finally:
            self.state = RoundState.IDLE
            await self._broadcast_status()

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    async def _run_step(self, step: dict) -> bool:
        """Create a session row and run the agent for one pipeline step.

        Returns True if the session ended due to an API error.
        """
        agent_type    = step.get('agent_type', 'claude')
        conv_id       = self.current_round.conversation_id
        round_index   = self.current_round.round_index
        step_index    = self.current_round.step_index
        is_resuming   = self.current_round.session_pk is not None
        resume_id     = self.current_round.resume_id

        if not is_resuming:
            # Create a fresh session row
            self.current_round.session_pk = await self.db.create_session(
                conv_id, round_index, step_index, agent_type
            )

        session_pk = self.current_round.session_pk

        # Broadcast status so frontend updates Round N-M display
        await self._broadcast_status()

        # Announce
        await self.broadcast({
            'type': 'session_created',
            'session': {
                'id':              session_pk,
                'agent_type':      agent_type,
                'round_index':     round_index,
                'step_index':      step_index,
                'total_steps':     len(self.current_round.pipeline_steps),
                'started_at':      datetime.now().isoformat(),
                'conversation_id': conv_id,
            },
        })
        await self.broadcast({
            'type': 'system',
            'content': (
                f'继续 Session {session_pk} - {agent_type.capitalize()}'
                if is_resuming else
                f'Round {round_index}-{step_index} - {agent_type.capitalize()} 运行中'
            ),
        })

        prompt    = await self._build_prompt(step)
        cwd       = self.current_round.working_dir
        summary   = None
        text_buf: list = []
        api_error = False

        # Periodic status broadcast
        _running = True

        async def _status_task():
            while _running:
                status = self.process_manager.get_status()
                if status:
                    await self.broadcast({
                        'type':       'session_status',
                        'session_id': session_pk,
                        **{k: round(v, 1) if isinstance(v, float) else v
                           for k, v in status.to_dict().items()},
                    })
                await asyncio.sleep(2)

        status_task = asyncio.create_task(_status_task())

        async def _flush_buf():
            nonlocal text_buf
            if text_buf:
                text = ''.join(text_buf)
                text_buf = []
                if text.strip():
                    await self.broadcast({
                        'type': 'agent_output', 'agent': agent_type,
                        'stream': 'stdout', 'content': text,
                    })

        try:
            async for out in self.process_manager.start_agent(
                agent_type, prompt,
                session_id=resume_id,
                working_dir_override=cwd,
            ):
                # Broadcast raw log line to frontend
                await self.broadcast({
                    'type':       'session_log',
                    'session_id': session_pk,
                    'stream':     out.stream,
                    'content':    out.content,
                    'event_type': out.event_type,
                    'timestamp':  datetime.now().isoformat(),
                })

                if out.stream == 'stderr':
                    await self.broadcast({
                        'type': 'agent_output', 'agent': agent_type,
                        'stream': 'stderr', 'content': out.content,
                    })

                elif out.stream == 'event':
                    # Live text extraction for streaming display
                    if agent_type == 'codex':
                        t = _extract_codex_text(out.content)
                        if t:
                            await self.broadcast({
                                'type': 'agent_output', 'agent': agent_type,
                                'stream': 'stdout', 'content': t,
                            })
                    elif agent_type == 'claude':
                        chunk = _extract_claude_text_delta(out.content)
                        if chunk:
                            text_buf.append(chunk)
                            joined = ''.join(text_buf)
                            if len(joined) >= 80 or joined[-1:] in '\n。！？.!?':
                                await _flush_buf()

                    # Process ID
                    if out.event_type == 'process_pid':
                        try:
                            pid = json.loads(out.content).get('pid')
                            if pid:
                                await self.broadcast({
                                    'type':       'session_pid',
                                    'session_id': session_pk,
                                    'pid':        pid,
                                })
                        except Exception:
                            pass

                    # Session init — capture cli_session_id for --resume
                    elif out.event_type in ('session_init', 'system', 'thread.started'):
                        try:
                            data   = json.loads(out.content)
                            cli_id = (
                                data.get('session_id')
                                or data.get('thread_id')
                                or (data.get('subtype') == 'init' and data.get('session_id'))
                            )
                            if cli_id and isinstance(cli_id, str):
                                self.current_round.resume_id = cli_id
                                await self.db.update_session_id(session_pk, cli_id)
                        except Exception:
                            pass

                    # Result event — detect API errors
                    elif out.event_type == 'result':
                        try:
                            data = json.loads(out.content)
                            if data.get('is_error'):
                                api_error = True
                        except Exception:
                            pass

                    # Done — capture summary
                    elif out.event_type == 'done':
                        await _flush_buf()
                        try:
                            data    = json.loads(out.content)
                            summary = data.get('summary')
                            cli_id  = data.get('session_id') or data.get('thread_id')
                            if cli_id:
                                self.current_round.resume_id = cli_id
                                await self.db.update_session_id(session_pk, cli_id)
                        except Exception:
                            pass

        finally:
            _running = False
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

            await _flush_buf()

            # Write summary (or NULL if killed / no output) into chat column
            # If summary is empty, try to extract from JSONL file
            if not summary:
                from . import session_reader
                cli_id = self.current_round.resume_id
                if cli_id:
                    extracted = session_reader.extract_session_summary(agent_type, cli_id)
                    if extracted:
                        summary = extracted
                        log.info(f"Extracted summary from JSONL for session {session_pk}")

            try:
                await self.db.update_session_chat(session_pk, summary)
            except Exception:
                pass

        # Broadcast the completed chat bubble
        await self.broadcast({
            'type':            'chat_message',
            'role':            agent_type,
            'content':         summary,
            'round_index':     round_index,
            'step_index':      step_index,
            'session_id':      session_pk,
            'conversation_id': conv_id,
            'timestamp':       datetime.now().isoformat(),
        })

        # Clear session_pk so the next step creates a fresh row
        self.current_round.session_pk = None

        return api_error

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _format_history(self, rows: list) -> str:
        """Format chat history rows from get_chat_history()."""
        if not rows:
            return '（暂无对话历史）'
        lines = []
        for r in rows:
            role     = {'codex': 'Codex', 'claude': 'Claude'}.get(r.get('role', ''), r.get('role', ''))
            ts       = r.get('timestamp', '')
            time_str = ''
            if ts:
                try:
                    time_str = datetime.fromisoformat(str(ts)).strftime('%H:%M')
                except Exception:
                    time_str = str(ts)[:5]
            lines.append(f'**{role}** [{time_str}]: {r.get("content") or ""}')
        return '\n'.join(lines)

    async def _build_prompt(self, step: dict) -> str:
        agent_type      = step.get('agent_type', 'claude')
        custom_template = step.get('prompt_template')
        conv_id         = self.current_round.conversation_id

        # When resuming, the agent already has session memory — skip history injection
        if self.current_round.resume_id:
            rows = []
        elif conv_id:
            # Use recent summaries (3 sessions) instead of full 40-message history
            rows = await self.db.get_recent_summaries(conv_id, limit=3)
        else:
            rows = []

        chat_history = self._format_history(rows)

        user_input = '无'
        if self.current_round.user_inputs:
            user_input = '\n'.join(f'- {u}' for u in self.current_round.user_inputs)
            self.current_round.user_inputs = []
        elif conv_id:
            latest = await self.db.get_latest_user_message(conv_id)
            if latest:
                user_input = f'- {latest}'

        # last_summary: last non-NULL chat in history
        last_summary = (rows[-1].get('content') if rows else None) or '（上一步骤未提供总结）'

        template = custom_template or DEFAULT_PROMPT_TEMPLATE

        try:
            return template.format(
                round_number  = self.current_round.round_index,
                chat_history  = chat_history,
                user_input    = user_input,
                codex_summary = last_summary,
                last_summary  = last_summary,
            )
        except KeyError as e:
            log.warning(f"Prompt template unknown variable {e}")
            return template.format_map({
                'round_number':  self.current_round.round_index,
                'chat_history':  chat_history,
                'user_input':    user_input,
                'codex_summary': last_summary,
                'last_summary':  last_summary,
            })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_step(self) -> Optional[dict]:
        steps = self.current_round.pipeline_steps
        if not steps:
            return None
        idx = self.current_round.step_index - 1   # step_index is 1-based
        if idx < 0 or idx >= len(steps):
            return None
        return steps[idx]

    async def _broadcast_status(self) -> None:
        steps = self.current_round.pipeline_steps
        current_step = self._current_step()
        agent = current_step.get('agent_type', 'agent') if current_step else None
        await self.broadcast({
            'type':        'agent_status',
            'state':       self.state.value,
            'round':       self.current_round.round_index,
            'step_index':  self.current_round.step_index,
            'total_steps': len(steps) if steps else 0,
            'agent':       agent,
        })

    async def _restore_from_db(self, conversation_id: int, for_retry: bool = False) -> bool:
        """Restore RoundData from DB after a server restart.

        continue (for_retry=False): position at the next unrun step (or next round).
        retry    (for_retry=True) : position at the last run step (never advances).
        """
        steps = await self.db.get_pipeline_steps(conversation_id) or [
            {'agent_type': 'codex',  'prompt_template': None},
            {'agent_type': 'claude', 'prompt_template': None},
        ]
        conv        = await self.db.get_conversation(conversation_id)
        working_dir = conv.get('working_dir') if conv else None

        latest = await self.db.get_latest_session(conversation_id)
        if not latest:
            return False

        cur_round = latest['round_index']
        cur_step  = latest['step_index']

        if for_retry:
            # Re-run the last step
            self.current_round = RoundData(
                round_index     = cur_round,
                step_index      = cur_step,
                conversation_id = conversation_id,
                pipeline_steps  = steps,
                working_dir     = working_dir,
            )
            return True

        # Continue: advance to next step
        next_step = cur_step + 1
        if next_step > len(steps):
            # All steps of this round done — open next round
            max_rounds = MAX_ROUNDS
            if max_rounds > 0 and cur_round >= max_rounds:
                await self.broadcast({
                    'type': 'system',
                    'content': f'已达最大轮次 {max_rounds}，自动停止',
                })
                return False
            next_round = cur_round + 1
            next_step  = 1
        else:
            next_round = cur_round

        self.current_round = RoundData(
            round_index     = next_round,
            step_index      = next_step,
            conversation_id = conversation_id,
            pipeline_steps  = steps,
            working_dir     = working_dir,
        )
        return True
