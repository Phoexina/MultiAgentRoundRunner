"""
Session Reader - Read agent session JSONL files from disk.

Mimics cc-switch's provider parsing approach:
- Claude: ~/.claude/projects/**/{session_id}.jsonl
- Codex:  ~/.openai/config/sessions/**/{thread_id}.jsonl (or subdirs)

Returns structured messages instead of raw streaming output.
"""

import json
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _claude_root() -> Path:
    return Path.home() / '.claude' / 'projects'


def _codex_root() -> Path:
    return Path.home() / '.codex' / 'sessions'


def _gemini_root() -> Path:
    return Path.home() / '.gemini'


def find_claude_session_file(cli_session_id: str) -> Optional[Path]:
    """
    Locate a Claude session JSONL by session ID.
    Claude names the file after the session UUID, so we glob for it.
    """
    root = _claude_root()
    if not root.exists():
        return None
    # Skip agent- prefixed files (sub-agent sessions)
    matches = [
        p for p in root.glob(f'**/{cli_session_id}.jsonl')
        if not p.name.startswith('agent-')
    ]
    return matches[0] if matches else None


def find_codex_session_file(cli_session_id: str) -> Optional[Path]:
    """Locate a Codex session JSONL by session ID.

    Codex stores session metadata in state_5.sqlite with the rollout_path.
    Query the database directly instead of globbing the filesystem.
    """
    import sqlite3

    db_path = Path.home() / '.codex' / 'state_5.sqlite'
    if not db_path.exists():
        log.warning(f"Codex state database not found: {db_path}")
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT rollout_path FROM threads WHERE id = ?", (cli_session_id,))
        row = cur.fetchone()
        conn.close()

        if row and row[0]:
            path = Path(row[0])
            if path.exists():
                return path
            else:
                log.warning(f"Codex session file not found: {path}")
                return None
        else:
            log.warning(f"Codex session {cli_session_id} not found in database")
            return None
    except Exception as e:
        log.error(f"Error querying Codex database: {e}")
        return None


def find_gemini_session_file(cli_session_id: str) -> Optional[Path]:
    """Locate a Gemini session file (JSONL or JSON) by sessionId."""
    tmp = _gemini_root() / 'tmp'
    if not tmp.exists():
        return None
    # JSONL (interactive mode) — sessionId is in the first line
    for path in tmp.glob('**/chats/session-*.jsonl'):
        try:
            first_line = path.read_text(encoding='utf-8', errors='replace').splitlines()[0]
            if json.loads(first_line).get('sessionId') == cli_session_id:
                return path
        except Exception:
            continue
    # JSON (non-interactive mode) — sessionId is at root level
    for path in tmp.glob('**/chats/session-*.json'):
        try:
            if json.loads(path.read_text(encoding='utf-8', errors='replace')).get('sessionId') == cli_session_id:
                return path
        except Exception:
            continue
    return None


def find_gemini_session_by_time(created_at: str, working_dir: Optional[str] = None) -> Optional[Path]:
    """Find the Gemini JSONL session file closest in time to created_at.

    Used when session_id was never stored in DB (sessions created before the JSONL fix).
    Parses the timestamp embedded in filenames: session-YYYY-MM-DDTHH-MM-<hash>.jsonl
    Only matches files within 5 minutes of created_at.
    """
    tmp = _gemini_root() / 'tmp'
    if not tmp.exists():
        return None

    import re
    from datetime import datetime, timezone

    try:
        ref = created_at.strip().replace(' ', 'T')
        if not ref.endswith('Z') and '+' not in ref:
            ref += '+00:00'
        ref_dt = datetime.fromisoformat(ref.replace('Z', '+00:00'))
        ref_ts = ref_dt.timestamp()
    except Exception:
        return None

    # Narrow to project folder when working_dir is known
    if working_dir:
        project_name = Path(working_dir).name
        candidate_root = tmp / project_name
        search_root = candidate_root if candidate_root.exists() else tmp
    else:
        search_root = tmp

    ts_pat = re.compile(r'session-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-')
    best_path: Optional[Path] = None
    best_diff = float('inf')

    for path in search_root.glob('**/chats/session-*.jsonl'):
        m = ts_pat.search(path.name)
        if not m:
            continue
        try:
            date_part, hh, mm = m.group(1), m.group(2), m.group(3)
            file_dt = datetime.fromisoformat(f"{date_part}T{hh}:{mm}:00+00:00")
            diff = abs(file_dt.timestamp() - ref_ts)
            if diff < best_diff and diff < 300:  # within 5 minutes
                best_diff = diff
                best_path = path
        except Exception:
            continue

    return best_path


def snapshot_gemini_sessions() -> set:
    """Return the set of all existing Gemini session file paths (used before process start)."""
    tmp = _gemini_root() / 'tmp'
    if not tmp.exists():
        return set()
    paths = set()
    for pattern in ('**/chats/session-*.jsonl', '**/chats/session-*.json'):
        paths.update(str(p) for p in tmp.glob(pattern))
    return paths


def find_latest_gemini_session_id(pre_paths: set) -> Optional[str]:
    """Find the sessionId of the newest Gemini session file not present in pre_paths."""
    tmp = _gemini_root() / 'tmp'
    if not tmp.exists():
        return None
    new_files = [
        p for pattern in ('**/chats/session-*.jsonl', '**/chats/session-*.json')
        for p in tmp.glob(pattern)
        if str(p) not in pre_paths
    ]
    if not new_files:
        return None
    newest = max(new_files, key=lambda p: p.stat().st_mtime)
    try:
        first_line = newest.read_text(encoding='utf-8', errors='replace').splitlines()[0]
        return json.loads(first_line).get('sessionId')
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_blocks_to_text(content) -> str:
    """Extract plain text from a content block list or string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text':
            t = block.get('text', '')
            if t:
                parts.append(t)
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Claude parser
# ---------------------------------------------------------------------------

def load_claude_messages(path: Path) -> list[dict]:
    """
    Parse a Claude session JSONL file into structured messages.

    Each returned dict has:
      role     : 'user' | 'assistant' | 'tool'
      type     : 'text' | 'tool_use' | 'tool_result' | 'thinking'
      content  : str   (main text or tool name)
      metadata : dict  (optional extra fields)
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.warning(f"Cannot read Claude session file {path}: {e}")
        return []

    messages: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        # Skip metadata and summary entries
        if event.get('isMeta') or event.get('type') in ('summary', 'result', 'system'):
            continue

        event_type = event.get('type')

        # ── User turn ──────────────────────────────────────────────────
        if event_type == 'user':
            message = event.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content', [])

            # Plain string content
            if isinstance(content, str) and content:
                messages.append({'role': 'user', 'type': 'text', 'content': content})
                continue

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                btype = block.get('type')

                if btype == 'text':
                    text_val = block.get('text', '')
                    if text_val:
                        messages.append({'role': 'user', 'type': 'text', 'content': text_val})

                elif btype == 'tool_result':
                    result_content = block.get('content', '')
                    if isinstance(result_content, list):
                        result_content = _content_blocks_to_text(result_content)
                    elif not isinstance(result_content, str):
                        result_content = json.dumps(result_content, ensure_ascii=False)
                    messages.append({
                        'role': 'tool',
                        'type': 'tool_result',
                        'content': result_content,
                        'metadata': {'tool_use_id': block.get('tool_use_id', '')},
                    })

        # ── Assistant turn ─────────────────────────────────────────────
        elif event_type == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content', [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                btype = block.get('type')

                if btype == 'text':
                    text_val = block.get('text', '')
                    if text_val:
                        messages.append({'role': 'assistant', 'type': 'text', 'content': text_val})

                elif btype == 'tool_use':
                    name = block.get('name', 'unknown_tool')
                    input_data = block.get('input') or {}
                    messages.append({
                        'role': 'assistant',
                        'type': 'tool_use',
                        'content': name,
                        'metadata': {
                            'tool_name': name,
                            'tool_input': input_data,
                            'tool_id': block.get('id', ''),
                        },
                    })

                elif btype == 'thinking':
                    thinking = block.get('thinking', '')
                    if thinking:
                        messages.append({'role': 'assistant', 'type': 'thinking', 'content': thinking})

    return messages


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------

def _codex_parse_timestamp(value) -> Optional[int]:
    """
    Convert a Codex timestamp to milliseconds.
    Accepts: int/float seconds or milliseconds, RFC3339 string.
    """
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 1_000_000_000_000 else n * 1000
    if isinstance(value, str):
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _codex_extract_text(content) -> str:
    """
    Recursively extract plain text from Codex payload content.
    Handles: str, list of content items, object with 'text' key.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _codex_extract_text_from_item(item)
            if text and text.strip():
                parts.append(text)
        return '\n'.join(parts)
    if isinstance(content, dict):
        return content.get('text', '')
    return ''


def _codex_extract_text_from_item(item: dict) -> Optional[str]:
    """Extract text from a single content array item."""
    if not isinstance(item, dict):
        return None

    item_type = item.get('type', '')

    if item_type == 'tool_use':
        name = item.get('name', 'unknown')
        return f'[Tool: {name}]'

    if item_type == 'tool_result':
        inner = item.get('content')
        if inner is not None:
            text = _codex_extract_text(inner)
            if text:
                return text
        return None

    for field in ('text', 'input_text', 'output_text'):
        val = item.get(field)
        if isinstance(val, str):
            return val

    inner = item.get('content')
    if inner is not None:
        text = _codex_extract_text(inner)
        if text:
            return text

    return None


def load_codex_messages(path: Path) -> list[dict]:
    """
    Parse a Codex session JSONL file (cc-switch format) into structured messages.

    Codex uses JSON Lines where each event has:
      type      : only "response_item" lines carry conversation content
      timestamp : RFC3339 string or int seconds/ms
      payload   : object with sub-type:
                    "message"              -> role + content (text/array)
                    "function_call"        -> assistant tool invocation
                    "function_call_output" -> tool result

    Each returned dict has:
      role     : 'user' | 'assistant' | 'tool'
      type     : 'text' | 'tool_use' | 'tool_result'
      content  : str
      metadata : dict (optional, for tool calls)
      ts       : int | None  (milliseconds since epoch)
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.warning(f"Cannot read Codex session file {path}: {e}")
        return []

    messages: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        if event.get('type') != 'response_item':
            continue

        payload = event.get('payload')
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get('type', '')
        ts = _codex_parse_timestamp(event.get('timestamp'))

        if payload_type == 'message':
            role = payload.get('role', 'unknown')
            content = _codex_extract_text(payload.get('content', ''))
            if not content.strip():
                continue
            messages.append({'role': role, 'type': 'text', 'content': content, 'ts': ts})

        elif payload_type == 'function_call':
            name = payload.get('name', 'unknown')
            content = f'[Tool: {name}]'
            messages.append({
                'role': 'assistant',
                'type': 'tool_use',
                'content': content,
                'metadata': {
                    'tool_name': name,
                    'tool_input': payload.get('arguments', ''),
                    'call_id': payload.get('call_id', ''),
                },
                'ts': ts,
            })

        elif payload_type == 'function_call_output':
            output = payload.get('output', '')
            if not output.strip():
                continue
            messages.append({
                'role': 'tool',
                'type': 'tool_result',
                'content': output,
                'metadata': {'call_id': payload.get('call_id', '')},
                'ts': ts,
            })

    return messages


# ---------------------------------------------------------------------------
# Gemini parser
# ---------------------------------------------------------------------------

def load_gemini_messages(path: Path) -> list[dict]:
    """Parse a Gemini session JSON file into structured messages.

    Gemini stores sessions as a single JSON object (not JSONL) at:
      ~/.gemini/tmp/<project_name>/chats/session-*.json

    Message types: "user" → role user, "gemini" → role assistant,
                   "info"/"error" → skipped.
    Content may be a plain string or a list of {text: "..."} objects.
    Gemini assistant messages may also have a "thoughts" list with reasoning steps.
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except Exception as e:
        log.warning(f"Cannot read Gemini session file {path}: {e}")
        return []

    messages: list[dict] = []
    for msg in data.get('messages', []):
        msg_type = msg.get('type', '')
        if msg_type in ('info', 'error'):
            continue
        role = 'assistant' if msg_type == 'gemini' else 'user'
        ts = _codex_parse_timestamp(msg.get('timestamp'))

        # Thoughts: Gemini's internal reasoning steps (shown before main response)
        if msg_type == 'gemini':
            for thought in (msg.get('thoughts') or []):
                if not isinstance(thought, dict):
                    continue
                desc = thought.get('description', '')
                if desc and desc.strip():
                    messages.append({'role': role, 'type': 'thinking', 'content': desc.strip(), 'ts': ts})

        content = msg.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))

        for call in (msg.get('toolCalls') or []):
            if isinstance(call, dict) and call.get('name'):
                content += f"\n[Tool: {call['name']}]"

        if not content.strip():
            continue

        messages.append({'role': role, 'type': 'text', 'content': content, 'ts': ts})

    return messages


# ---------------------------------------------------------------------------
# Summary extraction (for filling empty chat column)
# ---------------------------------------------------------------------------

def extract_session_summary(agent: str, cli_session_id: Optional[str]) -> Optional[str]:
    """
    Extract the last assistant message as a summary from a session JSONL file.

    Priority:
      1. Last assistant 'message' with non-empty text content
      2. If no assistant message, last non-tool message content

    Used when session chat column is NULL (e.g., killed before completion).
    Returns the extracted summary string, or None if not found.
    """
    if not cli_session_id:
        return None

    agent_lower = agent.lower()

    if agent_lower == 'claude':
        path = find_claude_session_file(cli_session_id)
        if path:
            return _extract_claude_summary(path)

    elif agent_lower == 'codex':
        path = find_codex_session_file(cli_session_id)
        if path:
            return _extract_codex_summary(path)

    elif agent_lower == 'gemini':
        path = find_gemini_session_file(cli_session_id)
        if path:
            if path.suffix == '.jsonl':
                return _extract_gemini_jsonl_summary(path)
            return _extract_gemini_summary(path)

    return None


def _extract_claude_summary(path: Path) -> Optional[str]:
    """
    Extract summary from Claude session JSONL.
    Reads the file backwards to find the last assistant text message.
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.warning(f"Cannot read Claude session file {path}: {e}")
        return None

    # Parse lines in reverse order
    lines = text.splitlines()
    last_assistant_text = None

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        # Skip metadata entries
        if event.get('isMeta') or event.get('type') in ('summary', 'result', 'system'):
            continue

        # Look for assistant messages
        if event.get('type') == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content', [])
            if not isinstance(content, list):
                continue

            # Extract text from content blocks (in order, first text block)
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text':
                    text_val = block.get('text', '')
                    if text_val and text_val.strip():
                        last_assistant_text = text_val.strip()
                        break

            if last_assistant_text:
                break  # Found the last assistant message

    return last_assistant_text


def _extract_codex_summary(path: Path) -> Optional[str]:
    """
    Extract summary from Codex session JSONL.
    Reads the file backwards to find the last assistant message.
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        log.warning(f"Cannot read Codex session file {path}: {e}")
        return None

    lines = text.splitlines()
    last_assistant_text = None

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        if event.get('type') != 'response_item':
            continue

        payload = event.get('payload')
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get('type', '')

        # Look for assistant messages
        if payload_type == 'message':
            role = payload.get('role', '')
            if role == 'assistant':
                content = _codex_extract_text(payload.get('content', ''))
                if content and content.strip():
                    last_assistant_text = content.strip()
                    break

    return last_assistant_text


def _extract_gemini_summary(path: Path) -> Optional[str]:
    """Extract the last assistant message from a Gemini session JSON as summary."""
    try:
        data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except Exception as e:
        log.warning(f"Cannot read Gemini session file {path}: {e}")
        return None

    for msg in reversed(data.get('messages', [])):
        if msg.get('type') == 'gemini':
            content = msg.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
            if content and content.strip():
                return content.strip()
    return None


def _extract_gemini_jsonl_summary(path: Path) -> Optional[str]:
    """Extract the last gemini text response from a Gemini JSONL session file."""
    msgs = load_gemini_jsonl_messages(path)
    for msg in reversed(msgs):
        if msg.get('role') == 'assistant' and msg.get('type') == 'text':
            return msg['content']
    return None


# ---------------------------------------------------------------------------
# Gemini JSONL parser (interactive mode)
# ---------------------------------------------------------------------------

def load_gemini_jsonl_messages(path: Path) -> list[dict]:
    """Parse a Gemini session JSONL file (interactive mode) into structured messages.

    Format: one JSON object per line.
    - Line 1: session header  {"sessionId":..., "kind":"main"}
    - Messages: {"id":"...", "type":"user"|"gemini", "content":..., "toolCalls":[...], "thoughts":[...]}
    - Metadata: {"$set":{...}}  — skipped
    - Same message ID may appear twice; last occurrence wins (has complete toolCalls data).
    """
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception as e:
        log.warning(f"Cannot read Gemini JSONL session file {path}: {e}")
        return []

    # De-duplicate by message id: last occurrence of each id wins
    seen: dict[str, dict] = {}
    ordered_ids: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if '$set' in obj or obj.get('kind') == 'main':
            continue
        msg_id = obj.get('id')
        if not msg_id:
            continue
        if msg_id not in seen:
            ordered_ids.append(msg_id)
        seen[msg_id] = obj

    messages: list[dict] = []

    for msg_id in ordered_ids:
        msg = seen[msg_id]
        msg_type = msg.get('type', '')
        if msg_type in ('info', 'error'):
            continue

        role = 'assistant' if msg_type == 'gemini' else 'user'
        ts = _codex_parse_timestamp(msg.get('timestamp'))

        # Thoughts
        for thought in (msg.get('thoughts') or []):
            if not isinstance(thought, dict):
                continue
            desc = thought.get('description', '')
            if desc and desc.strip():
                messages.append({'role': role, 'type': 'thinking', 'content': desc.strip(), 'ts': ts})

        # Tool calls (gemini messages that called tools)
        for call in (msg.get('toolCalls') or []):
            if not isinstance(call, dict) or not call.get('name'):
                continue
            name = call.get('displayName') or call.get('name')
            messages.append({
                'role': 'assistant',
                'type': 'tool_use',
                'content': name,
                'metadata': {
                    'tool_name': call.get('name'),
                    'tool_input': call.get('args', {}),
                    'tool_id': call.get('id', ''),
                },
                'ts': ts,
            })
            # Tool result
            rd = call.get('resultDisplay', '')
            if isinstance(rd, dict):
                rd = rd.get('summary') or json.dumps(rd, ensure_ascii=False)
            if isinstance(rd, str) and rd.strip():
                messages.append({
                    'role': 'tool',
                    'type': 'tool_result',
                    'content': rd,
                    'metadata': {'tool_name': call.get('name', '')},
                    'ts': ts,
                })

        # Main content (text response)
        content = msg.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
        if content.strip():
            messages.append({'role': role, 'type': 'text', 'content': content, 'ts': ts})

    return messages


# ---------------------------------------------------------------------------
# Gemini session patcher
# ---------------------------------------------------------------------------

def patch_gemini_session_response(path: Path, response_text: str) -> bool:
    """Append a gemini assistant message to a Gemini session JSON file.

    Called after the CLI exits when the session file only contains the user
    message (non-interactive mode never writes the response back).
    Returns True if the file was successfully patched, False otherwise.
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except Exception as e:
        log.warning(f"Cannot read Gemini session file for patching {path}: {e}")
        return False

    messages = data.get('messages', [])
    if not messages or messages[-1].get('type') != 'user':
        # Already has a gemini reply or is empty — skip
        return False

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    messages.append({
        'type': 'gemini',
        'content': response_text,
        'timestamp': ts,
    })
    data['messages'] = messages

    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        log.info(f"Patched Gemini session file with response: {path}")
        return True
    except Exception as e:
        log.warning(f"Cannot write Gemini session file {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_session_messages(agent: str, cli_session_id: Optional[str]) -> dict:
    """
    Load structured messages for a session identified by agent type and CLI session ID.

    Returns:
        {
            'messages': list[dict],   # structured messages
            'source': 'file' | 'not_found',
            'path': str | None,       # absolute path to the source file
        }
    """
    if not cli_session_id:
        return {'messages': [], 'source': 'not_found', 'path': None}

    agent_lower = agent.lower()

    if agent_lower == 'claude':
        path = find_claude_session_file(cli_session_id)
        if path:
            msgs = load_claude_messages(path)
            return {'messages': msgs, 'source': 'file', 'path': str(path)}

    elif agent_lower == 'codex':
        path = find_codex_session_file(cli_session_id)
        if path:
            msgs = load_codex_messages(path)
            return {'messages': msgs, 'source': 'file', 'path': str(path)}

    elif agent_lower == 'gemini':
        path = find_gemini_session_file(cli_session_id)
        if path:
            loader = load_gemini_jsonl_messages if path.suffix == '.jsonl' else load_gemini_messages
            msgs = loader(path)
            return {'messages': msgs, 'source': 'file', 'path': str(path)}

    log.info(f"Session file not found: agent={agent}, cli_session_id={cli_session_id}")
    return {'messages': [], 'source': 'not_found', 'path': None}
