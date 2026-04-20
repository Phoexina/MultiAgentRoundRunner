"""
Process Manager - Agent subprocess lifecycle management
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import time
from typing import AsyncIterator, Optional
from dataclasses import dataclass
from datetime import datetime

from .cli_types import ProcessResult, is_cli_error, is_cli_timeout, is_parse_error
from .stream_parser import parse_ndjson, read_lines

import logging
log = logging.getLogger(__name__)

AGENT_COMMANDS: dict[str, str] = {
    'codex':  'codex',
    'claude': 'claude',
}


@dataclass
class AgentOutput:
    """Output from agent process"""
    stream: str  # 'stdout', 'stderr', 'event'
    content: str
    event_type: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


@dataclass
class ProcessStatus:
    """Runtime status for a running process, sent to frontend for display."""
    pid: int
    start_time: float  # time.time() when process started
    last_output_time: float  # time.time() of most recent stdout/stderr

    def to_dict(self) -> dict:
        now = time.time()
        return {
            'pid': self.pid,
            'start_time': self.start_time,
            'last_output_time': self.last_output_time,
            'duration': now - self.start_time,
            'idle': now - self.last_output_time,
        }


class AgentProcess:
    """
    Manages the subprocess lifecycle for a single agent.
    MCP is NOT injected here — each CLI discovers it from its own working directory:
      Claude Code: reads .mcp.json in cwd
      Codex CLI:   reads .codex/config.toml in cwd
    """

    def __init__(self, agent_name: str, command: str, working_dir: str = '.'):
        self.agent_name  = agent_name
        self.command     = command
        self.working_dir = working_dir

        self.process: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self._killed = False
        self._pending_logs: list[AgentOutput] = []
        self._status: Optional[ProcessStatus] = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def get_status(self) -> Optional[ProcessStatus]:
        """Return current process status for UI display."""
        return self._status

    def _build_claude_command(self, prompt: str, session_id: Optional[str] = None) -> tuple[list[str], str]:
        """
        Build Claude Code CLI command.
        Claude reads .mcp.json from cwd automatically — no --mcp-config needed.
        """
        cmd = [
            self.command,
            '-p',
            '--output-format', 'stream-json',
            '--include-partial-messages',
            '--verbose',
            '--permission-mode', 'bypassPermissions',
        ]
        if session_id:
            cmd.extend(['--resume', session_id])
        return cmd, prompt

    def _build_codex_command(self, prompt: str, session_id: Optional[str] = None) -> tuple[list[str], str]:
        """
        Build Codex CLI command.
        Prompt is passed via stdin (using '-') to avoid shell escaping issues with multi-line prompts.
        Codex reads .codex/config.toml from cwd automatically.
        """
        cmd = [self.command, 'exec']
        if session_id:
            cmd.extend(['resume', session_id])
        cmd.extend(['--skip-git-repo-check', '-s', 'danger-full-access', '--json', '--config', 'approval_policy="on-request"', '--', '-'])
        return cmd, prompt

    def _escape_shell_args(self, args: list[str]) -> list[str]:
        """Escape shell arguments for Windows cmd"""
        escaped = []
        for arg in args:
            if ' ' in arg or '"' in arg or '&' in arg or '|' in arg:
                escaped.append(f'"{arg.replace(chr(34), chr(34)+chr(34))}"')
            else:
                escaped.append(arg)
        return escaped

    async def start(self, prompt: str, session_id: Optional[str] = None) -> None:
        """Start the agent subprocess."""
        if shutil.which(self.command) is None:
            log.warning(f"{self.agent_name} CLI not found, using mock mode")
            await self._run_mock(prompt)
            return

        if self.agent_name == 'claude':
            cmd, stdin_content = self._build_claude_command(prompt, session_id)
        elif self.agent_name == 'codex':
            cmd, stdin_content = self._build_codex_command(prompt, session_id)
        else:
            raise ValueError(f"Unknown agent: {self.agent_name}")

        cwd = self.working_dir
        log.info(f"Starting {self.agent_name} in {cwd}: {' '.join(cmd[:6])}...")

        needs_stdin = bool(stdin_content)

        if sys.platform == 'win32':
            shell_cmd = ' '.join(self._escape_shell_args(cmd))
            # Inherit parent environment so PATH, API keys etc. are available,
            # then override encoding vars to force UTF-8 from Python subprocesses.
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            self.process = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdin=asyncio.subprocess.PIPE if needs_stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        else:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if needs_stdin else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

        if needs_stdin and self.process.stdin:
            # stdin expects bytes; stdout/stderr will be decoded in ndjson_parser
            self.process.stdin.write((stdin_content + '\n').encode('utf-8'))
            await self.process.stdin.drain()
            self.process.stdin.close()

        log.info(f"{self.agent_name} started with PID {self.process.pid}")

        # Initialize status tracking
        self._status = ProcessStatus(
            pid=self.process.pid,
            start_time=time.time(),
            last_output_time=time.time(),
        )

    async def _kill_process_tree(self) -> None:
        """Kill the process and all its children.

        On Windows, asyncio.create_subprocess_shell() spawns cmd.exe which in
        turn spawns the actual agent binary.  terminate()/kill() on the shell
        only kills cmd.exe; the child keeps running and consuming API tokens.
        taskkill /F /T /PID kills the entire tree atomically.
        On Unix, SIGKILL the process group so child processes share the fate.
        """
        if not self.process:
            return
        pid = self.process.pid
        if sys.platform == 'win32':
            try:
                killer = await asyncio.create_subprocess_exec(
                    'taskkill', '/F', '/T', '/PID', str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
                log.info(f"taskkill /F /T /PID {pid} done")
            except Exception as e:
                log.error(f"taskkill failed for PID {pid}: {e}")
                with contextlib.suppress(Exception):
                    self.process.kill()
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                log.info(f"Killed process group of PID {pid}")
            except ProcessLookupError:
                pass  # already dead
            except Exception as e:
                log.error(f"killpg failed for PID {pid}: {e}")
                with contextlib.suppress(Exception):
                    self.process.kill()

    async def stop(self) -> None:
        """Stop the agent process and its entire process tree."""
        if self.is_running():
            pid = self.process.pid
            log.info(f"Stopping {self.agent_name} (PID {pid})...")
            await self._kill_process_tree()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning(f"{self.agent_name} (PID {pid}) still alive after kill, force-killing")
                self.process.kill()
                await self.process.wait()
        self._killed = True

    async def _run_mock(self, prompt: str) -> None:
        """Run in mock mode when CLI is not available"""
        mock_outputs = [
            AgentOutput('stdout', f'[MOCK] {self.agent_name} CLI not found. Running in mock mode.'),
            AgentOutput('stdout', f'[MOCK] Prompt received ({len(prompt)} chars)'),
            AgentOutput('stdout', '[MOCK] Simulating agent response...'),
            AgentOutput('event', json.dumps({
                'type': 'session_init',
                'session_id': f'mock-{self.agent_name}-{datetime.now().timestamp()}'
            }), 'session_init'),
            AgentOutput('stdout', f'[SUMMARY]Mock {self.agent_name} round completed.[/SUMMARY]'),
        ]
        self.session_id = f'mock-{self.agent_name}'
        for output in mock_outputs:
            self._pending_logs.append(output)
            await asyncio.sleep(0.2)

    async def stream_output(self) -> AsyncIterator[AgentOutput]:
        """Stream output from the agent process.

        No automatic timeout kill — the process runs until it completes or user stops it.
        Process status (PID, duration, idle time) is tracked for UI display.
        """
        if not self.process:
            # Mock mode: yield buffered outputs
            for output in self._pending_logs:
                yield output
            return

        # Announce PID so the frontend can display it
        yield AgentOutput('event', json.dumps({'type': 'process_pid', 'pid': self.process.pid}), 'process_pid')

        # Single merged queue: ('stdout', event_dict) | ('stderr', line) | ('_done_stdout',) | ('_done_stderr',)
        merged: asyncio.Queue = asyncio.Queue()
        _DONE_STDOUT = object()
        _DONE_STDERR = object()

        async def read_stdout():
            try:
                async for event in parse_ndjson(self.process.stdout):
                    await merged.put(('stdout', event))
            except Exception as e:
                log.error(f"Error reading stdout: {e}")
            finally:
                await merged.put(('_sentinel', _DONE_STDOUT))

        async def read_stderr():
            try:
                async for line in read_lines(self.process.stderr):
                    await merged.put(('stderr', line))
            except Exception as e:
                log.error(f"Error reading stderr: {e}")
            finally:
                await merged.put(('_sentinel', _DONE_STDERR))

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        stdout_done = False
        stderr_done = False

        try:
            while not (stdout_done and stderr_done):
                # No timeout — wait forever for output. User decides when to kill.
                kind, data = await merged.get()

                # Update last output time for status tracking
                if self._status:
                    self._status = ProcessStatus(
                        pid=self._status.pid,
                        start_time=self._status.start_time,
                        last_output_time=time.time(),
                    )

                if kind == '_sentinel':
                    if data is _DONE_STDOUT:
                        stdout_done = True
                    elif data is _DONE_STDERR:
                        stderr_done = True
                    continue

                if kind == 'stderr':
                    yield AgentOutput('stderr', data)
                    continue

                # kind == 'stdout': event dict
                event = data
                if is_parse_error(event):
                    yield AgentOutput('stderr', f"Parse error: {event.get('line', '')}")
                    continue
                if is_cli_timeout(event):
                    yield AgentOutput('event', json.dumps(event), 'timeout')
                    continue
                if is_cli_error(event):
                    yield AgentOutput('event', json.dumps(event), 'error')
                    continue

                yield AgentOutput('event', json.dumps(event, ensure_ascii=False), event.get('type'))

                # Extract session_id from session_init events
                # Claude: type='system', subtype='init' -> session_id
                # Codex: type='thread.started' -> thread_id
                if isinstance(event, dict):
                    if event.get('type') == 'system' and event.get('subtype') == 'init':
                        sid = event.get('session_id')
                        if isinstance(sid, str):
                            self.session_id = sid
                    elif event.get('type') == 'thread.started':
                        tid = event.get('thread_id')
                        if isinstance(tid, str):
                            self.session_id = tid

        finally:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    async def wait(self) -> ProcessResult:
        """Wait for the process to complete."""
        if not self.process:
            return ProcessResult(exit_code=0, session_id=self.session_id, summary=self._extract_summary(), error=None)

        exit_code = await self.process.wait()
        summary = self._extract_summary()
        return ProcessResult(
            exit_code=exit_code,
            session_id=self.session_id,
            summary=summary,
            error=f"Process exited with code {exit_code}" if exit_code != 0 else None
        )

    def _extract_summary(self) -> Optional[str]:
        """Extract [SUMMARY]...[/SUMMARY] from logs.

        For stdout/stderr entries, searches the raw text directly.
        For event entries, JSON-decodes first so that escape sequences
        (\\r\\n, \\uXXXX, etc.) are resolved before pattern matching.
        """
        import re
        pattern = re.compile(r'\[SUMMARY\](.*?)\[/SUMMARY\]', re.DOTALL)

        collected_texts: list[str] = []

        for output in self._pending_logs:
            # For non-event streams (stdout/stderr), do a direct text search.
            # For event streams, output.content is JSON-serialized — searching it
            # directly would return JSON escape sequences (e.g. literal \r\n instead
            # of real newlines, \uXXXX instead of Chinese chars), so we skip the
            # direct search here and rely on the JSON-decoded paths below.
            if output.stream != 'event':
                m = pattern.search(output.content)
                if m:
                    return m.group(1).strip()

            # Try decoding JSON and searching text fields
            if output.stream == 'event':
                try:
                    event = json.loads(output.content)
                    # Codex: item.completed / agent_message
                    item = event.get('item') if isinstance(event, dict) else None
                    if isinstance(item, dict):
                        text = item.get('text') or ''
                        if text:
                            collected_texts.append(text)
                            m = pattern.search(text)
                            if m:
                                return m.group(1).strip()
                        # Codex: message / content[]
                        for part in (item.get('content') or []):
                            if isinstance(part, dict) and part.get('type') == 'output_text':
                                t = part.get('text') or ''
                                if t:
                                    collected_texts.append(t)
                                    m = pattern.search(t)
                                    if m:
                                        return m.group(1).strip()
                    # Claude: assistant message content blocks
                    message = event.get('message') if isinstance(event, dict) else None
                    if isinstance(message, dict):
                        for block in (message.get('content') or []):
                            if isinstance(block, dict) and block.get('type') == 'text':
                                t = block.get('text') or ''
                                if t:
                                    collected_texts.append(t)
                                    m = pattern.search(t)
                                    if m:
                                        return m.group(1).strip()
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: return last non-trivial text chunk
        for text in reversed(collected_texts):
            if len(text) > 10:
                return text[:500] + ('...' if len(text) > 500 else '')

        for output in reversed(self._pending_logs):
            if output.stream in ('stdout', 'event') and output.content and len(output.content) > 10:
                return output.content[:500] + ('...' if len(output.content) > 500 else '')

        return f"{self.agent_name.capitalize()} completed this round"

    def add_log(self, output: AgentOutput) -> None:
        self._pending_logs.append(output)


class ProcessManager:
    """Central manager for all agent processes. Supports concurrent parallel runs."""

    def __init__(self):
        self._active_processes: list[AgentProcess] = []

    @property
    def is_running(self) -> bool:
        return any(p.is_running() for p in self._active_processes)

    def get_status(self) -> Optional[ProcessStatus]:
        """Return status of the first running process (for backward compat)."""
        for p in self._active_processes:
            s = p.get_status()
            if s:
                return s
        return None

    async def start_agent(
        self,
        agent_name: str,
        prompt: str,
        session_id: Optional[str] = None,
        working_dir_override: Optional[str] = None
    ) -> AsyncIterator[AgentOutput]:
        """Start an agent and stream its output. Multiple concurrent calls are allowed."""
        command = AGENT_COMMANDS.get(agent_name, agent_name)
        process = AgentProcess(agent_name, command, working_dir=working_dir_override or '.')
        self._active_processes.append(process)

        try:
            await process.start(
                prompt=prompt,
                session_id=session_id,
            )

            async for output in process.stream_output():
                process.add_log(output)
                yield output

            result = await process.wait()

            yield AgentOutput(
                'event',
                json.dumps({
                    'type': 'done',
                    'exit_code': result.exit_code,
                    'session_id': result.session_id,
                    'summary': result.summary,
                    'error': result.error
                }),
                'done'
            )

        finally:
            self._active_processes = [p for p in self._active_processes if p is not process]

    async def stop_current(self) -> None:
        """Stop all currently running agent processes."""
        await asyncio.gather(*[p.stop() for p in self._active_processes], return_exceptions=True)

    def get_current_session_id(self) -> Optional[str]:
        for p in self._active_processes:
            if p.session_id:
                return p.session_id
        return None
