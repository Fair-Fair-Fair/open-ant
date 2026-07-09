# 新增: src/ant/tools/policy.py
from __future__ import annotations
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ant.core.agent import AgentSession

logger = logging.getLogger(__name__)


@dataclass
class ToolPolicy:
    """Defines constraints and permissions for tool usage."""
    allowed_tools: set[str] | None = None       # None = all allowed
    denied_tools: set[str] = field(default_factory=set)
    max_calls_per_turn: dict[str, int] = field(default_factory=dict)
    max_calls_per_session: dict[str, int] = field(default_factory=dict)
    require_confirmation: set[str] = field(default_factory=set)
    param_validators: dict[str, callable] = field(default_factory=dict)


class ToolGovernance:
    """Enforces tool usage policies with rate limiting and audit."""

    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self._session_call_counts: dict[str, int] = defaultdict(int)
        self._turn_call_counts: dict[str, int] = defaultdict(int)
        self._audit_log: list[dict[str, Any]] = []

    def check_permission(self, tool_name: str, session: "AgentSession") -> tuple[bool, str]:
        """Check if a tool call is allowed."""
        policy = self.policy

        if policy.allowed_tools is not None and tool_name not in policy.allowed_tools:
            return False, f"Tool '{tool_name}' not in allowed list"

        if tool_name in policy.denied_tools:
            return False, f"Tool '{tool_name}' is denied"

        session_limit = policy.max_calls_per_session.get(tool_name)
        if session_limit and self._session_call_counts[tool_name] >= session_limit:
            return False, f"Tool '{tool_name}' exceeded session limit ({session_limit})"

        turn_limit = policy.max_calls_per_turn.get(tool_name)
        if turn_limit and self._turn_call_counts[tool_name] >= turn_limit:
            return False, f"Tool '{tool_name}' exceeded per-turn limit ({turn_limit})"

        return True, ""

    def record_call(self, tool_name: str, args: dict, result: str, elapsed: float) -> None:
        """Record a tool call for audit."""
        self._session_call_counts[tool_name] += 1
        self._turn_call_counts[tool_name] += 1
        self._audit_log.append({
            "tool": tool_name,
            "args": args,
            "result_preview": result[:200],
            "elapsed": elapsed,
            "timestamp": time.time(),
        })

    def reset_turn_counts(self) -> None:
        self._turn_call_counts.clear()

    def get_audit_summary(self) -> dict[str, Any]:
        return {
            "total_calls": sum(self._session_call_counts.values()),
            "calls_by_tool": dict(self._session_call_counts),
            "recent_log": self._audit_log[-10:],
        }
