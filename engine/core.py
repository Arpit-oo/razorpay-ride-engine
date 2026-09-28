from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


# Base error for the engine; messages are fixed text and never carry user data.
class EngineError(Exception):
    message = "engine error"

    # Always use the class-level safe message, ignoring any caller-supplied detail.
    def __init__(self) -> None:
        super().__init__(self.message)


# Raised when an input value is malformed or out of range.
class ValidationError(EngineError):
    message = "invalid input"


# Raised when the caller is not allowed to perform the operation.
class NotAllowedError(EngineError):
    message = "operation not allowed for caller"


# Raised when a ride is asked to move to a state it cannot reach from its current one.
class InvalidTransitionError(EngineError):
    message = "invalid state transition"


# Raised when a quote or offer is used after its time ran out.
class ExpiredError(EngineError):
    message = "quote or offer expired"


# Raised when a requested item does not exist.
class NotFoundError(EngineError):
    message = "item not found"


# Source of current time in whole seconds, injectable so tests control time.
class Clock(Protocol):
    # Return current time as integer seconds.
    def now(self) -> int: ...


# Manually driven clock for deterministic tests; safe to share across threads.
class FixedClock:
    # Start the clock at a given non-negative second.
    def __init__(self, start: int = 0) -> None:
        if not is_int(start) or start < 0:
            raise ValidationError()
        self._now = start
        self._lock = threading.Lock()

    # Read current time.
    def now(self) -> int:
        with self._lock:
            return self._now

    # Move time forward; going backwards is rejected.
    def advance(self, seconds: int) -> None:
        if not is_int(seconds) or seconds < 0:
            raise ValidationError()
        with self._lock:
            self._now += seconds


# A cell on the integer city grid.
@dataclass(frozen=True)
class Point:
    x: int
    y: int

    # Reject non-integer coordinates (bool counts as non-integer here).
    def __post_init__(self) -> None:
        if not is_int(self.x) or not is_int(self.y):
            raise ValidationError()

    # Hide exact coordinates when printed, since locations are sensitive.
    def __repr__(self) -> str:
        return "Point(<hidden>)"


# Grid distance between two points: |dx| + |dy|.
def manhattan(a: Point, b: Point) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


# Hands out unique sequential ids like "ride-1"; thread-safe counter.
class IdGenerator:
    # Prefix must be a non-empty string.
    def __init__(self, prefix: str) -> None:
        if not isinstance(prefix, str) or not prefix:
            raise ValidationError()
        self._prefix = prefix
        self._counter = 0
        self._lock = threading.Lock()

    # Return the next id; lock held only for the increment.
    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            value = self._counter
        return f"{self._prefix}-{value}"


# True only for real ints, excluding bool.
def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
