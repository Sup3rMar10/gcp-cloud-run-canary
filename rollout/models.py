from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Target:
    """One entry in a Cloud Run service's traffic split."""

    revision: str
    percent: int


@dataclass
class ServiceState:
    traffic: tuple[Target, ...]
    ready_revisions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Observation:
    """Requests a revision served in a window, and how many of them failed (5xx)."""

    requests: int
    errors: int

    @property
    def error_ratio(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


class DeployError(Exception):
    """The new revision could not be created or never became ready."""


class RolloutError(Exception):
    pass


class CloudRun(Protocol):
    def describe(self, service: str) -> ServiceState: ...
    def deploy(self, service: str, image: str, tag: str) -> str: ...  # creates a revision with NO traffic; returns its name
    def tag_url(self, service: str, tag: str) -> str: ...
    def set_traffic(self, service: str, targets: list[Target]) -> None: ...
    def remove_tag(self, service: str, tag: str) -> None: ...


class Metrics(Protocol):
    def observe(self, service: str, revision: str, window_seconds: int) -> Observation: ...


@dataclass
class SLO:
    max_error_ratio: float = 0.01  # absolute ceiling on the candidate's 5xx ratio
    relative_factor: float = 2.0  # and no worse than this multiple of the current revision's own ratio
    min_requests: int = 50  # fewer requests than this cannot prove anything


@dataclass
class RolloutResult:
    status: str  # promoted | rolled_back | aborted | rollback_failed
    reason: str
    revision: str | None = None
    previous: str | None = None
    steps_completed: list[int] = field(default_factory=list)
    final_traffic: tuple[Target, ...] = ()
