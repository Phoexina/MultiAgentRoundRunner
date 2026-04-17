"""
Stream Parser - Parse real-time stdout/stderr from agent subprocesses.

parse_ndjson : stdout — chunk-based NDJSON parser, yields parsed event dicts
read_lines   : stderr — raw line reader
"""

import json
from typing import AsyncIterator, Any

# Maximum line length to process (truncate longer lines)
MAX_LINE_LENGTH = 100000


async def parse_ndjson(stream: Any) -> AsyncIterator[Any]:
    """
    Parse a stream of NDJSON (newline-delimited JSON) into
    an async iterable of parsed objects.

    Blank lines are silently skipped.
    Lines that fail JSON.parse are yielded as ParseError objects.
    Very long lines are truncated to avoid memory issues.

    Uses chunk-based reading to avoid asyncio StreamReader line length limits.

    Args:
        stream: An async iterable of bytes or strings (e.g., process.stdout)

    Yields:
        Parsed JSON objects or error objects for unparseable lines
    """
    buffer = ""

    # Use read() instead of iteration to avoid line length limits
    while True:
        try:
            # Read chunks of up to 1MB at a time
            chunk = await stream.read(1024 * 1024)
            if not chunk:
                break
        except Exception as e:
            # Log but don't propagate - keep the stream alive
            import logging
            logging.getLogger(__name__).warning(f"Error reading stream chunk: {e}")
            break

        # Decode chunk to string if needed
        if isinstance(chunk, bytes):
            chunk = chunk.decode('utf-8', errors='replace')

        buffer += chunk

        # Process complete lines
        while '\n' in buffer:
            try:
                line, buffer = buffer.split('\n', 1)
            except Exception as e:
                # Line too long - truncate buffer and continue
                import logging
                logging.getLogger(__name__).warning(f"Line split error, truncating: {e}")
                line = buffer[:MAX_LINE_LENGTH] + "...[TRUNCATED]"
                buffer = buffer[MAX_LINE_LENGTH:]
                # Try to find next newline
                if '\n' in buffer:
                    _, buffer = buffer.split('\n', 1)

            line = line.strip()

            if not line:
                continue

            # Truncate very long lines
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "...[TRUNCATED]"

            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {
                    "__parseError": True,
                    "line": line[:500] if len(line) > 500 else line,  # Truncate in error too
                    "error": "Failed to parse JSON line"
                }
            except Exception as e:
                yield {
                    "__parseError": True,
                    "line": line[:500] if len(line) > 500 else line,
                    "error": f"Error processing line: {e}"
                }

    # Process any remaining content in buffer
    if buffer.strip():
        line = buffer.strip()
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + "...[TRUNCATED]"
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            yield {
                "__parseError": True,
                "line": line[:500] if len(line) > 500 else line,
                "error": "Failed to parse JSON line"
            }
        except Exception as e:
            yield {
                "__parseError": True,
                "line": line[:500] if len(line) > 500 else line,
                "error": f"Error processing line: {e}"
            }


async def read_lines(stream: Any) -> AsyncIterator[str]:
    """
    Read lines from a stream, yielding raw string lines.
    Uses chunk-based reading to avoid asyncio StreamReader line length limits.
    Truncates very long lines to avoid memory issues.

    Args:
        stream: An async iterable of bytes (e.g., process.stdout)

    Yields:
        Raw string lines (without newline characters)
    """
    buffer = ""

    # Use read() instead of iteration to avoid line length limits
    while True:
        try:
            chunk = await stream.read(1024 * 1024)
            if not chunk:
                break
        except Exception as e:
            # Log but don't propagate - keep the stream alive
            import logging
            logging.getLogger(__name__).warning(f"Error reading stream chunk: {e}")
            break

        if isinstance(chunk, bytes):
            chunk = chunk.decode('utf-8', errors='replace')

        buffer += chunk

        while '\n' in buffer:
            try:
                line, buffer = buffer.split('\n', 1)
            except Exception as e:
                # Line too long - truncate
                import logging
                logging.getLogger(__name__).warning(f"Line split error, truncating: {e}")
                line = buffer[:MAX_LINE_LENGTH] + "...[TRUNCATED]"
                buffer = buffer[MAX_LINE_LENGTH:]
                if '\n' in buffer:
                    _, buffer = buffer.split('\n', 1)

            # Truncate very long lines
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "...[TRUNCATED]"

            yield line.rstrip('\r')

    if buffer:
        if len(buffer) > MAX_LINE_LENGTH:
            buffer = buffer[:MAX_LINE_LENGTH] + "...[TRUNCATED]"
        yield buffer
