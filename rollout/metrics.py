"""Where the evidence comes from: Cloud Monitoring's request count for a revision, split by response class.

`monitoring_filter` builds the filter string Cloud Monitoring expects for the Cloud Run request-count metric. It is
tested as a string; a live query was never run. `StaticMetrics` is a scripted stand-in for tests and demos.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Observation


def monitoring_filter(service: str, revision: str, response_class: str | None = None) -> str:
    parts = [
        'metric.type = "run.googleapis.com/request_count"',
        'resource.type = "cloud_run_revision"',
        f'resource.labels.service_name = "{service}"',
        f'resource.labels.revision_name = "{revision}"',
    ]
    if response_class:
        parts.append(f'metric.labels.response_code_class = "{response_class}"')
    return " AND ".join(parts)


@dataclass
class StaticMetrics:
    """Scripted observations: revision -> list of Observations, consumed in order (the last one repeats)."""

    script: dict[str, list[Observation]]
    seen: list[tuple[str, int]] = field(default_factory=list)
    _i: dict[str, int] = field(default_factory=dict)

    def observe(self, service: str, revision: str, window_seconds: int) -> Observation:
        self.seen.append((revision, window_seconds))
        seq = self.script[revision]
        i = self._i.get(revision, 0)
        self._i[revision] = i + 1
        return seq[min(i, len(seq) - 1)]
