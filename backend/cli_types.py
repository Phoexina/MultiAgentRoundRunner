"""
CLI Types - Type definitions for process results
"""

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class ProcessResult:
    """Result from a completed agent process"""
    exit_code: int
    session_id: Optional[str] = None  # For Claude --resume
    summary: Optional[str] = None
    error: Optional[str] = None


# Sentinel types for CLI errors

def make_cli_error(exit_code: int, message: str, command: str) -> dict:
    """Create a CLI error sentinel object"""
    return {
        "__cliError": True,
        "exitCode": exit_code,
        "message": message,
        "command": command
    }


def make_cli_timeout(timeout_ms: int, message: str, command: str) -> dict:
    """Create a CLI timeout sentinel object"""
    return {
        "__cliTimeout": True,
        "timeoutMs": timeout_ms,
        "message": message,
        "command": command
    }


def is_cli_error(obj: Any) -> bool:
    """Type guard for CLI error objects"""
    return isinstance(obj, dict) and obj.get("__cliError") is True


def is_cli_timeout(obj: Any) -> bool:
    """Type guard for CLI timeout objects"""
    return isinstance(obj, dict) and obj.get("__cliTimeout") is True


def is_parse_error(obj: Any) -> bool:
    """Type guard for JSON parse error objects"""
    return isinstance(obj, dict) and obj.get("__parseError") is True
