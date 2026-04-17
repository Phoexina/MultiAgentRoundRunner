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
    # Codex CLI stores sessions at ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    return Path.home() / '.codex' / 'sessions'


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
    """
    Locate a Codex session JSONL by session ID.

    Codex names files as rollout-{datetime}-{UUID}.jsonl under date-partitioned
    subdirectories (YYYY/MM/DD/).  We glob for any .jsonl whose name contains
    the session UUID rather than matching the full filename.
    """
    root = _codex_root()
    if not root.exists():
        return None
    matches = [p for p in root.glob('**/*.jsonl') if cli_session_id in p.name]
    return matches[0] if matches else None


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

    log.info(f"Session file not found: agent={agent}, cli_session_id={cli_session_id}")
    return {'messages': [], 'source': 'not_found', 'path': None}
