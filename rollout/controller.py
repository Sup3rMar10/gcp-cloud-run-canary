"""A gradual rollout for Cloud Run that decides from evidence and always has a way back.

  1. deploy the new revision with NO traffic and a tag (its tagged URL works, real users do not reach it)
  2. smoke-test the tagged URL; a failure ends the rollout with traffic untouched
  3. shift traffic in steps (default 10, 50, 100 percent), letting each step bake and then comparing the new
     revision's 5xx ratio with an absolute ceiling AND with the current revision's own ratio
  4. too little traffic to judge counts as a failure, not a pass: no evidence is not good news
  5. on any failure, send 100% back to the revision that was serving (retried), and remove the tag
Every traffic change is read back and checked, because a call that returned success but did not take effect is how
a rollback silently fails.
"""

from __future__ import annotations

import time
from typing import Callable

from .models import (SLO, CloudRun, DeployError, Metrics, Observation, RolloutResult, Target)

TAG = "candidate"


class Rollout:
    def __init__(self, client: CloudRun, metrics: Metrics, smoke: Callable[[str], tuple[bool, str]],
                 steps: tuple[int, ...] = (10, 50, 100), bake_seconds: int = 300, max_extensions: int = 2,
                 slo: SLO | None = None, sleep: Callable[[float], None] = time.sleep, rollback_attempts: int = 3):
        if not steps or steps[-1] != 100 or list(steps) != sorted(set(steps)) or steps[0] < 1:
            raise ValueError("steps must be increasing percentages, starting above 0 and ending at 100")
        self.client, self.metrics, self.smoke = client, metrics, smoke
        self.steps, self.bake, self.max_extensions = steps, bake_seconds, max_extensions
        self.slo = slo or SLO()
        self.sleep = sleep
        self.rollback_attempts = rollback_attempts

    # --- helpers ---------------------------------------------------------------------------------------

    def _apply(self, service: str, targets: list[Target]) -> None:
        """Set the traffic split and read it back; raise unless it is exactly what was asked for."""
        self.client.set_traffic(service, targets)
        want = {(t.revision, t.percent) for t in targets if t.percent > 0}
        got = {(t.revision, t.percent) for t in self.client.describe(service).traffic}
        if want != got:
            raise RuntimeError(f"traffic did not take effect: wanted {sorted(want)}, got {sorted(got)}")

    def _rollback(self, service: str, previous: str, why: str, revision: str, done: list[int]) -> RolloutResult:
        last = ""
        for _ in range(self.rollback_attempts):
            try:
                self._apply(service, [Target(previous, 100)])
                self.client.remove_tag(service, TAG)
                return RolloutResult("rolled_back", why, revision, previous, done, self.client.describe(service).traffic)
            except Exception as e:  # noqa: BLE001 - keep trying; the situation is already bad
                last = str(e)
                self.sleep(1)
        return RolloutResult("rollback_failed", f"{why}; AND the rollback failed after {self.rollback_attempts} attempts ({last}): "
                             "traffic may still be on the new revision, intervene manually", revision, previous, done,
                             self.client.describe(service).traffic)

    def _judge(self, service: str, revision: str, previous: str, pct: int) -> tuple[bool, str]:
        for extension in range(self.max_extensions + 1):
            window = self.bake * (extension + 1)
            self.sleep(self.bake)
            cand = self.metrics.observe(service, revision, window)
            if cand.requests >= self.slo.min_requests:
                break
        else:
            return False, f"only {cand.requests} requests at {pct}% (need {self.slo.min_requests}): not enough traffic to judge"
        base = self.metrics.observe(service, previous, window)
        # The candidate is held to the SLO ceiling. If the current revision is itself over it (a bad dependency is
        # hurting the whole service) the ceiling relaxes to a multiple of the current revision's ratio, so a canary is
        # not blamed for an outage it did not cause. A baseline with too little traffic is ignored.
        limit = self.slo.max_error_ratio
        if base.requests >= self.slo.min_requests:
            limit = max(limit, base.error_ratio * self.slo.relative_factor)
        if cand.error_ratio > limit:
            return False, f"error ratio {cand.error_ratio:.2%} at {pct}% is over the limit {limit:.2%} (current revision: {base.error_ratio:.2%})"
        return True, ""

    # --- the rollout -----------------------------------------------------------------------------------

    def run(self, service: str, image: str) -> RolloutResult:
        state = self.client.describe(service)
        serving = max(state.traffic, key=lambda t: t.percent).revision if state.traffic else None
        try:
            revision = self.client.deploy(service, image, TAG)
        except DeployError as e:
            return RolloutResult("aborted", f"deploy failed, traffic untouched: {e}", None, serving,
                                 final_traffic=self.client.describe(service).traffic)

        ok, detail = self.smoke(self.client.tag_url(service, TAG))
        if not ok:
            self.client.remove_tag(service, TAG)
            return RolloutResult("aborted", f"smoke test failed on the tagged URL, traffic untouched: {detail}", revision, serving,
                                 final_traffic=self.client.describe(service).traffic)

        if serving is None:  # first ever revision: nothing to compare with or roll back to
            self.client.remove_tag(service, TAG)
            return RolloutResult("promoted", "first revision, so there was nothing to canary against", revision, None, [100],
                                 self.client.describe(service).traffic)

        done: list[int] = []
        for pct in self.steps:
            try:
                targets = [Target(revision, pct)] + ([Target(serving, 100 - pct)] if pct < 100 else [])
                self._apply(service, targets)
                if pct == 100:
                    done.append(pct)
                    break
                good, why = self._judge(service, revision, serving, pct)
            except Exception as e:  # noqa: BLE001
                return self._rollback(service, serving, f"error while shifting traffic to {pct}%: {e}", revision, done)
            if not good:
                return self._rollback(service, serving, why, revision, done)
            done.append(pct)
        self.client.remove_tag(service, TAG)
        return RolloutResult("promoted", f"healthy through {done}", revision, serving, done, self.client.describe(service).traffic)
