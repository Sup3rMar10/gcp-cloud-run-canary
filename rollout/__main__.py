"""python -m rollout simulate [good|bad-at-50|quiet|smoke-fails|deploy-fails|first] : watch a rollout against the fake Cloud Run.

(There is no real-service command: the gcloud client exists but was never run against a live project.)
"""

from __future__ import annotations

import argparse

from . import FakeCloudRun, Observation, Rollout, StaticMetrics

SCENARIOS = {
    "good": "a healthy release: 10%, 50%, 100%, promoted",
    "bad-at-50": "fine at 10%, but errors appear at 50%: rolled back",
    "quiet": "not enough traffic to judge: rolled back rather than promoted on no evidence",
    "smoke-fails": "the tagged URL fails its smoke test: traffic is never touched",
    "deploy-fails": "the new revision never becomes ready: traffic is never touched",
    "first": "the first ever revision: nothing to canary against",
}


def build(name: str):
    ok, bad = Observation(1000, 2), Observation(1000, 120)
    prev_metrics = [Observation(1000, 1)]
    cand = {"good": [ok], "bad-at-50": [ok, bad], "quiet": [Observation(5, 0)]}.get(name, [ok])
    fake = FakeCloudRun(serving=None if name == "first" else "orders-00001-aaa", fail_deploy=(name == "deploy-fails"))
    metrics = StaticMetrics({"orders-00002-abc": cand, "orders-00001-aaa": prev_metrics})

    def smoke(url: str):
        return (False, "GET /healthz returned 503") if name == "smoke-fails" else (True, "")

    return fake, Rollout(fake, metrics, smoke, bake_seconds=300, sleep=lambda s: None)


def main() -> int:
    p = argparse.ArgumentParser(prog="rollout")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate")
    s.add_argument("scenario", nargs="?", default="good", choices=sorted(SCENARIOS))
    d = sub.add_parser("deploy", help="roll out to a REAL Cloud Run service (never run live by the author)")
    d.add_argument("--project", required=True)
    d.add_argument("--region", required=True)
    d.add_argument("--service", required=True)
    d.add_argument("--image", required=True)
    d.add_argument("--bake-seconds", type=int, default=300)
    a = p.parse_args()
    if a.cmd == "deploy":
        from .gcloud import GcloudCloudRun
        from .live import MonitoringMetrics, http_smoke, identity_token

        ro = Rollout(GcloudCloudRun(a.project, a.region), MonitoringMetrics(a.project),
                     lambda url: http_smoke(url, token=identity_token()), bake_seconds=a.bake_seconds)
        result = ro.run(a.service, a.image)
        print(f"{result.status.upper()}: {result.reason}")
        return 0 if result.status == "promoted" else 1
    fake, ro = build(a.scenario)
    print(f"scenario: {SCENARIOS[a.scenario]}")
    result = ro.run("orders", "gcr.io/acme/orders:2")
    for call in fake.calls:
        if call[0] == "set_traffic":
            print("  traffic ->", ", ".join(f"{t.revision}={t.percent}%" for t in call[1]))
        else:
            print("  ", call[0], *call[1:])
    print(f"result: {result.status.upper()}: {result.reason}")
    print("final traffic:", ", ".join(f"{t.revision}={t.percent}%" for t in result.final_traffic))
    return 0 if result.status in ("promoted",) else 1


if __name__ == "__main__":
    raise SystemExit(main())
