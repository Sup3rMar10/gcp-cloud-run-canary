"""The real client: drives Cloud Run through the gcloud CLI. Never run against a live project.

The JSON shapes parsed here (status.traffic[].revisionName/percent, status.latestReadyRevisionName, the tag URL) are
the ones Cloud Run's API documents. They are tested against fixtures and a fake command runner, which checks the
exact flags that matter (--no-traffic, --tag, --to-revisions), not against a live service.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from .models import DeployError, ServiceState, Target

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


class GcloudCloudRun:
    def __init__(self, project: str, region: str, runner: Runner = _run):
        self.project, self.region, self.runner = project, region, runner

    def _base(self, *args: str) -> list[str]:
        return ["gcloud", "run", *args, "--project", self.project, "--region", self.region]

    def _service_json(self, service: str) -> dict:
        return json.loads(self.runner(self._base("services", "describe", service, "--format", "json")))

    def describe(self, service: str) -> ServiceState:
        try:
            doc = self._service_json(service)
        except subprocess.CalledProcessError:  # the service does not exist yet
            return ServiceState(())
        status = doc.get("status", {})
        traffic = tuple(Target(t["revisionName"], int(t.get("percent", 0))) for t in status.get("traffic", [])
                        if t.get("revisionName") and int(t.get("percent", 0)) > 0)
        return ServiceState(traffic, frozenset([status["latestReadyRevisionName"]] if status.get("latestReadyRevisionName") else []))

    def deploy(self, service: str, image: str, tag: str) -> str:
        try:
            # --no-traffic: the new revision is created but receives nothing; --tag gives it its own URL.
            self.runner(self._base("deploy", service, "--image", image, "--no-traffic", "--tag", tag, "--quiet"))
        except subprocess.CalledProcessError as e:
            raise DeployError(e.stderr or str(e)) from e
        doc = self._service_json(service)
        for t in doc.get("status", {}).get("traffic", []):
            if t.get("tag") == tag and t.get("revisionName"):
                return t["revisionName"]
        raise DeployError(f"deployed, but no revision carries the tag {tag!r}")

    def tag_url(self, service: str, tag: str) -> str:
        for t in self._service_json(service).get("status", {}).get("traffic", []):
            if t.get("tag") == tag and t.get("url"):
                return t["url"]
        raise DeployError(f"no URL for the tag {tag!r}")

    def set_traffic(self, service: str, targets: list[Target]) -> None:
        split = ",".join(f"{t.revision}={t.percent}" for t in targets)
        self.runner(self._base("services", "update-traffic", service, "--to-revisions", split))

    def remove_tag(self, service: str, tag: str) -> None:
        self.runner(self._base("services", "update-traffic", service, "--remove-tags", tag))
