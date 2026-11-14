"""Provider-specific authentication, fetch, and response adapters."""

from .claude import ClaudeAdapter
from .codex import CodexAdapter

DEFAULT_ADAPTERS = (CodexAdapter(), ClaudeAdapter())

__all__ = ["ClaudeAdapter", "CodexAdapter", "DEFAULT_ADAPTERS"]
