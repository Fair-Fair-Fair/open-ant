# 新增: src/ant/core/session_fsm.py
from enum import Enum
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime


class SessionPhase(Enum):
    """Explicit session lifecycle phases."""
    CREATED = "created"
    ACTIVE = "active"
    WAITING_TOOL = "waiting_tool"
    WAITING_INPUT = "waiting_input"
    COMPACTING = "compacting"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


TRANSITIONS: dict[SessionPhase, set[SessionPhase]] = {
    SessionPhase.CREATED: {SessionPhase.ACTIVE},
    SessionPhase.ACTIVE: {
        SessionPhase.WAITING_TOOL,
        SessionPhase.COMPACTING,
        SessionPhase.COMPLETED,
        SessionPhase.FAILED,
    },
    SessionPhase.WAITING_TOOL: {SessionPhase.ACTIVE},
    SessionPhase.WAITING_INPUT: {SessionPhase.ACTIVE},
    SessionPhase.COMPACTING: {SessionPhase.ACTIVE, SessionPhase.FAILED},
    SessionPhase.COMPLETED: set(),
    SessionPhase.FAILED: {SessionPhase.ACTIVE},
    SessionPhase.EXHAUSTED: set(),
}


@dataclass
class SessionFSM:
    """Finite state machine for session lifecycle."""
    phase: SessionPhase = SessionPhase.CREATED
    transition_count: int = 0
    phase_history: list[tuple[SessionPhase, float]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def transition_to(self, new_phase: SessionPhase) -> None:
        allowed = TRANSITIONS.get(self.phase, set())
        if new_phase not in allowed:
            raise ValueError(
                f"Invalid transition: {self.phase.value} -> {new_phase.value}. "
                f"Allowed: {[p.value for p in allowed]}"
            )
        self.phase_history.append((self.phase, datetime.now().timestamp()))
        self.phase = new_phase
        self.transition_count += 1
