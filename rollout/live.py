"""The parts that talk to the outside world: an HTTP smoke test and a Cloud Monitoring metrics reader.

`http_smoke` is tested against a real local HTTP server. `MonitoringMetrics` is NOT tested live (it needs a Google
Cloud project and the google-cloud-monitoring library); only the filter it sends is tested, as a string.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request

from .metrics import monitoring_filter
from .models import Observation


def identity_token() -> str:
    """An ID token for the calling identity, for a tagged URL that requires authentication."""
    return subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True).stdout.strip()


def http_smoke(url: str, path: str = "/healthz", token: str | None = None, timeout: float = 10.0, attempts: int = 5,
               pause: float = 2.0, sleep=time.sleep) -> tuple[bool, str]:
    """GET url+path until it answers 200, a few times (a fresh revision may need a moment). Returns (ok, detail)."""
    last = "no attempt made"
    for i in range(attempts):
        req = urllib.request.Request(url.rstrip("/") + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the URL comes from Cloud Run
                if resp.status == 200:
                    return True, ""
                last = f"GET {path} returned {resp.status}"
        except urllib.error.HTTPError as e:
            last = f"GET {path} returned {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"GET {path} failed: {e}"
        if i < attempts - 1:
            sleep(pause)
    return False, last


class MonitoringMetrics:
    """Requests and 5xx for a revision over the last `window_seconds`, from Cloud Monitoring. Untested live."""

    def __init__(self, project: str):
        self.project = project

    def observe(self, service: str, revision: str, window_seconds: int) -> Observation:
        from google.cloud import monitoring_v3  # imported here so the rest of the package needs no Google libraries

        client = monitoring_v3.MetricServiceClient()
        now = int(time.time())
        interval = monitoring_v3.TimeInterval({"start_time": {"seconds": now - window_seconds}, "end_time": {"seconds": now}})
        aggregation = monitoring_v3.Aggregation({
            "alignment_period": {"seconds": window_seconds},
            "per_series_aligner": monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            "cross_series_reducer": monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
            "group_by_fields": ["metric.labels.response_code_class"],
        })
        total = errors = 0
        for series in client.list_time_series(request={
            "name": f"projects/{self.project}", "filter": monitoring_filter(service, revision), "interval": interval,
            "aggregation": aggregation, "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }):
            count = sum(int(p.value.int64_value) for p in series.points)
            total += count
            if series.metric.labels.get("response_code_class") == "5xx":
                errors += count
        return Observation(total, errors)
