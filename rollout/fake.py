"""An in-memory Cloud Run that enforces the API rules the controller must respect.

It models, from Google's documented behaviour (not from a live service):
  - a new revision created with --no-traffic gets 0% and can be reached only through its tag URL
  - traffic percentages must be whole numbers that sum to exactly 100, over revisions that exist and are ready
  - a tag points at one revision; tags do not carry traffic
  - the service starts with no revision at all (the first deploy)
If the controller ever asks for something Cloud Run would refuse, this raises, so a controller bug shows up in a test
instead of in production. It is a model: real Cloud Run has more rules and failure modes than are listed here.
"""

from __future__ import annotations

from .models import DeployError, ServiceState, Target


class CloudRunRefused(Exception):
    """What a real Cloud Run API call would reject."""


class FakeCloudRun:
    def __init__(self, serving: str | None = None, fail_deploy: bool = False, flaky_traffic_calls: int = 0,
                 new_revision: str | None = None):
        self.new_revision = new_revision  # the name the next deploy produces (default: generated)
        self.fail_deploy = fail_deploy
        self.flaky_traffic_calls = flaky_traffic_calls  # the next N set_traffic calls fail transiently
        self.revisions: dict[str, str] = {}  # name -> image
        self.ready: set[str] = set()
        self.traffic: tuple[Target, ...] = ()
        self.tags: dict[str, str] = {}  # tag -> revision
        self.calls: list[tuple] = []
        self._n = 0
        if serving:
            self.revisions[serving] = "image:" + serving
            self.ready.add(serving)
            self.traffic = (Target(serving, 100),)

    def describe(self, service: str) -> ServiceState:
        return ServiceState(self.traffic, frozenset(self.ready))

    def deploy(self, service: str, image: str, tag: str) -> str:
        self.calls.append(("deploy", image, tag))
        if self.fail_deploy:
            raise DeployError("the revision failed its startup probe and never became ready")
        self._n += 1
        name = self.new_revision or f"{service}-0000{self._n + len(self.revisions)}-abc"
        self.revisions[name] = image
        self.ready.add(name)
        self.tags[tag] = name
        if not self.traffic:  # the very first revision has nothing to share traffic with
            self.traffic = (Target(name, 100),)
        return name

    def tag_url(self, service: str, tag: str) -> str:
        if tag not in self.tags:
            raise CloudRunRefused(f"no tag {tag!r}")
        return f"https://{tag}---{service}-hash-uc.a.run.app"

    def set_traffic(self, service: str, targets: list[Target]) -> None:
        self.calls.append(("set_traffic", tuple(targets)))
        if self.flaky_traffic_calls > 0:
            self.flaky_traffic_calls -= 1
            raise RuntimeError("503 backend error")
        if not targets:
            raise CloudRunRefused("traffic must name at least one revision")
        if any(not isinstance(t.percent, int) or t.percent < 0 or t.percent > 100 for t in targets):
            raise CloudRunRefused("percentages must be whole numbers from 0 to 100")
        if sum(t.percent for t in targets) != 100:
            raise CloudRunRefused(f"traffic must sum to 100, got {sum(t.percent for t in targets)}")
        for t in targets:
            if t.revision not in self.revisions:
                raise CloudRunRefused(f"revision {t.revision!r} does not exist")
            if t.revision not in self.ready:
                raise CloudRunRefused(f"revision {t.revision!r} is not ready")
        self.traffic = tuple(t for t in targets if t.percent > 0)

    def remove_tag(self, service: str, tag: str) -> None:
        self.calls.append(("remove_tag", tag))
        self.tags.pop(tag, None)

    # helpers for assertions
    def percent(self, revision: str) -> int:
        return sum(t.percent for t in self.traffic if t.revision == revision)
