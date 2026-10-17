# gcp-cloud-run-canary

![ci](https://github.com/Sup3rMar10/gcp-cloud-run-canary/actions/workflows/ci.yml/badge.svg)

A **gradual rollout controller for Cloud Run** that decides from evidence and always has a way back: deploy the new revision with **no traffic**, smoke-test its **tagged URL**, shift **10% then 50% then 100%**, judge each step against an error-rate SLO, and **roll back automatically**. Plus a small service that honours the Cloud Run container contract, checked on the real image in CI.

> **What is verified, and what is not.** 57 tests pass locally on Python 3.13 (1 more runs only on Linux), including the controller against a fake Cloud Run that enforces the API's rules, the `gcloud` command construction, and an HTTP smoke test against a real local server. I broke the controller's guards on purpose four times and tests failed each time. **Nothing has touched Google Cloud**: no project, no real Cloud Run, no live Cloud Monitoring. The fake is *my model* of documented behaviour, the `gcloud` JSON shapes are from the API documentation, and the metrics reader (`rollout/live.py`) was never run live. The container, however, is real: CI builds it, runs it, and checks the contract.

## The rollout (`rollout/controller.py`)

1. **Deploy with no traffic** (`--no-traffic --tag candidate`): the revision exists and has its own URL; real users do not reach it.
2. **Smoke test the tagged URL.** A failure ends the rollout with traffic **untouched**.
3. **Shift in steps** (default 10, 50, 100). Each step bakes, then the candidate's 5xx ratio is judged.
4. **Roll back** on any failure: 100% back to the revision that was serving, retried, then the tag removed.

| Rule | Behaviour |
|---|---|
| Evidence | The candidate's 5xx ratio must stay under an absolute ceiling (default 1%). If the *current* revision is itself over it, the ceiling relaxes to 2x that ratio, so a canary is not blamed for an outage hurting the whole service. A baseline with too little traffic is ignored |
| No evidence is not good news | Fewer than 50 requests at a step cannot prove anything: it waits longer (3 windows), then **rolls back** rather than promoting blind |
| Read-back | Every traffic change is read back and compared. A call that "succeeded" but did not take effect is caught, then rolled back |
| Rollback can fail | It is retried; if it cannot complete the result is `rollback_failed`, says traffic may still be on the new revision, and reports where traffic really is. It never reports success |
| Previous revision | Never deleted by the controller |
| First ever deploy | Promoted without a canary: nothing to compare with |

`python -m rollout simulate good|bad-at-50|quiet|smoke-fails|deploy-fails|first` replays each case against the fake.

## What the tests cover

- **The fake enforces Cloud Run's rules** (traffic must sum to exactly 100, over revisions that exist and are ready; a new revision starts at 0%), so a controller bug becomes a test failure, and a test proves the fake itself refuses invalid splits.
- 32 controller tests: promotion and the exact 10/50/100 splits, the smoke test running before any traffic and on the tagged URL, aborts that leave traffic exactly as it was, rollback at 10% and at 50%, the insufficient-traffic path, the SLO boundary (1% passes, 1.1% does not), the relaxed ceiling, a flaky and a permanently failing rollback, a client that lies about applying traffic, and nonsense step lists.
- The `gcloud` client: every command names the project and region; deploy uses `--no-traffic --tag`; the revision is found **by its tag, not guessed**; a split is `--to-revisions A=10,B=90`; a missing service describes as empty.
- The **HTTP smoke test** against a real local server: passes on 200, fails with the status on 503, tolerates a slow start, sends a bearer token, and fails cleanly when nothing is listening.
- The service: health, version, a `FAIL_EVERY=N` switch that plays a bad release, structured JSON logs with `severity`, the right port, and (on Linux) SIGTERM draining to exit 0.

## The container contract, on the real image (CI `container` job)

Builds `app/Dockerfile`, runs it with `PORT=9090` and checks it listens there and serves `/healthz`; does **not** run as root; on `docker stop` drains and **exits 0 in under 6 seconds** (Cloud Run allows about 10) and logs "stopped cleanly"; and passes a Trivy gate on fixable CRITICAL vulnerabilities.

## Honest limits

- **Never run against Google Cloud.** Real Cloud Run has behaviours the fake does not (revision creation latency, traffic propagation delay, quota, ingress and IAM on the tagged URL, regional differences). The first live run is the real test.
- **Metrics are the weak link.** Cloud Monitoring data lags by a minute or more, so a short bake window can judge stale data. `MonitoringMetrics` was never run; only its filter string is tested.
- **A canary needs traffic.** Splitting by percentage on a low-traffic service gives noisy ratios; the minimum-request rule makes it fail safe, but then a quiet service may roll back healthy releases.
- It judges **error rate only**. Latency, saturation and business metrics would matter in production.
- The 5xx ratio treats all errors alike and includes errors from a bad dependency (partly handled by the relaxed ceiling, which is a judgement call, not a law).
- The `deploy` command and `deploy.yml` are reference code, never run. `service.yaml` was never applied.
- Rollouts for one service are not serialised by the controller (the workflow's `concurrency` group does that); two concurrent runs would fight.

## Run it

```
python -m pip install pytest pyyaml
python -m pytest
python -m rollout simulate bad-at-50
docker build -t orders app && docker run --rm -e PORT=8080 -p 8080:8080 orders
```
