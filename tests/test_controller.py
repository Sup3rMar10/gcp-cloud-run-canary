import pytest

from rollout import (SLO, CloudRunRefused, FakeCloudRun, Observation, Rollout, StaticMetrics, Target)

PREV, NEW = "orders-00001-aaa", "orders-00002-abc"
OK = Observation(1000, 2)  # 0.2% errors over 1000 requests
BAD = Observation(1000, 120)  # 12%


def make(cand=(OK,), prev=(Observation(1000, 1),), fake=None, smoke=None, sleeps=None, **kw):
    fake = fake or FakeCloudRun(serving=PREV, new_revision=NEW)
    metrics = StaticMetrics({NEW: list(cand), PREV: list(prev)})
    sleeps = sleeps if sleeps is not None else []
    ro = Rollout(fake, metrics, smoke or (lambda url: (True, "")), sleep=sleeps.append, **kw)
    return fake, metrics, ro, sleeps


def run(fake, ro):
    return ro.run("orders", "image:new")


# --- the healthy path -------------------------------------------------------------------------------------


def test_a_healthy_release_is_promoted_in_steps_and_ends_pinned_to_the_new_revision():
    fake, _, ro, _ = make()
    r = run(fake, ro)
    assert r.status == "promoted" and r.steps_completed == [10, 50, 100]
    assert r.final_traffic == (Target(NEW, 100),)
    assert r.revision == NEW and r.previous == PREV


def test_traffic_moves_exactly_10_then_50_then_100():
    fake, _, ro, _ = make()
    run(fake, ro)
    splits = [c[1] for c in fake.calls if c[0] == "set_traffic"]
    assert splits == [(Target(NEW, 10), Target(PREV, 90)), (Target(NEW, 50), Target(PREV, 50)), (Target(NEW, 100),)]


def test_the_new_revision_gets_no_traffic_until_after_the_smoke_test():
    order = []
    fake = FakeCloudRun(serving=PREV, new_revision=NEW)
    real_set = fake.set_traffic
    fake.set_traffic = lambda s, t: (order.append("traffic"), real_set(s, t))[1]
    fake2, _, ro, _ = make(fake=fake, smoke=lambda url: (order.append("smoke") or True, ""))
    run(fake2, ro)
    assert order[0] == "smoke" and order.count("traffic") == 3


def test_the_smoke_test_hits_the_tagged_url_not_the_service_url():
    seen = []
    fake, _, ro, _ = make(smoke=lambda url: (seen.append(url) or True, ""))
    run(fake, ro)
    assert seen == [f"https://candidate---orders-hash-uc.a.run.app"]


def test_each_step_bakes_before_it_is_judged_and_the_last_step_is_not_judged():
    fake, _, ro, sleeps = make(bake_seconds=120)
    run(fake, ro)
    assert sleeps == [120, 120]  # judged at 10% and 50%; 100% is the end


def test_the_candidate_is_measured_before_the_previous_revision_at_each_step():
    fake, metrics, ro, _ = make()
    run(fake, ro)
    assert [rev for rev, _ in metrics.seen] == [NEW, PREV, NEW, PREV]


def test_the_tag_is_removed_and_the_previous_revision_is_kept_for_a_later_rollback():
    fake, _, ro, _ = make()
    run(fake, ro)
    assert "candidate" not in fake.tags
    assert PREV in fake.revisions, "the controller never deletes the revision it might need to go back to"


def test_the_first_ever_revision_is_promoted_without_a_canary():
    fake, _, ro, sleeps = make(fake=FakeCloudRun(serving=None, new_revision=NEW))
    r = run(fake, ro)
    assert r.status == "promoted" and r.previous is None and sleeps == []
    assert len(fake.traffic) == 1 and fake.traffic[0].percent == 100


def test_custom_steps_are_honoured():
    fake, _, ro, sleeps = make(steps=(25, 100))
    r = run(fake, ro)
    assert r.steps_completed == [25, 100] and len(sleeps) == 1


# --- things that stop it before any user is affected ------------------------------------------------------------


def test_a_deploy_failure_leaves_traffic_exactly_as_it_was():
    fake, _, ro, _ = make(fake=FakeCloudRun(serving=PREV, fail_deploy=True, new_revision=NEW))
    r = run(fake, ro)
    assert r.status == "aborted" and "never became ready" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and r.final_traffic == (Target(PREV, 100),)
    assert not [c for c in fake.calls if c[0] == "set_traffic"]


def test_a_failed_smoke_test_aborts_without_touching_traffic_and_cleans_the_tag():
    fake, _, ro, _ = make(smoke=lambda url: (False, "GET /healthz returned 503"))
    r = run(fake, ro)
    assert r.status == "aborted" and "503" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and "candidate" not in fake.tags
    assert not [c for c in fake.calls if c[0] == "set_traffic"]


# --- rolling back on evidence ----------------------------------------------------------------------------------


def test_errors_at_10_percent_roll_back_to_the_previous_revision():
    fake, _, ro, _ = make(cand=(BAD,))
    r = run(fake, ro)
    assert r.status == "rolled_back" and r.steps_completed == [] and "10%" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and "candidate" not in fake.tags


def test_errors_that_only_appear_at_50_percent_still_roll_back_and_record_progress():
    fake, _, ro, _ = make(cand=(OK, BAD))
    r = run(fake, ro)
    assert r.status == "rolled_back" and r.steps_completed == [10] and "50%" in r.reason
    assert fake.percent(PREV) == 100 and fake.percent(NEW) == 0


def test_not_enough_traffic_is_a_failure_not_a_pass():
    fake, metrics, ro, _ = make(cand=(Observation(5, 0),), bake_seconds=300)
    r = run(fake, ro)
    assert r.status == "rolled_back" and "not enough traffic" in r.reason and "5 requests" in r.reason
    assert [w for rev, w in metrics.seen if rev == NEW] == [300, 600, 900], "it waits longer before giving up"


def test_a_quiet_start_that_picks_up_in_time_is_judged_on_the_larger_window():
    fake, _, ro, sleeps = make(cand=(Observation(10, 0), Observation(200, 0)))
    r = run(fake, ro)
    assert r.status == "promoted" and sleeps[:2] == [300, 300]


@pytest.mark.parametrize("cand_errors,promoted", [(1, True), (10, True), (11, False), (80, False)])
def test_the_absolute_ceiling_is_one_percent_when_the_current_revision_is_healthy(cand_errors, promoted):
    fake, _, ro, _ = make(cand=(Observation(1000, cand_errors),), prev=(Observation(1000, 1),))
    assert run(fake, ro).status == ("promoted" if promoted else "rolled_back")


def test_a_candidate_is_not_blamed_for_an_outage_the_whole_service_is_having():
    """Current revision at 5%: the ceiling relaxes to 2x that, so 8% passes and 12% does not."""
    unhealthy = Observation(1000, 50)
    fake, _, ro, _ = make(cand=(Observation(1000, 80),), prev=(unhealthy,))
    assert run(fake, ro).status == "promoted"
    fake, _, ro, _ = make(cand=(Observation(1000, 120),), prev=(unhealthy,))
    assert run(fake, ro).status == "rolled_back"


def test_a_baseline_with_too_little_traffic_is_ignored_not_trusted():
    noisy_baseline = Observation(5, 5)  # 100% errors, but over 5 requests: meaningless
    fake, _, ro, _ = make(cand=(Observation(1000, 20),), prev=(noisy_baseline,))
    assert run(fake, ro).status == "rolled_back"  # 2% is over the 1% ceiling; the junk baseline did not excuse it


def test_a_stricter_slo_can_be_set():
    fake, _, ro, _ = make(cand=(Observation(1000, 5),), slo=SLO(max_error_ratio=0.001))
    assert run(fake, ro).status == "rolled_back"


# --- when the rollback itself is in trouble ----------------------------------------------------------------------


class RollbackFlaky(FakeCloudRun):
    """Fails the first N attempts to send everything back to the previous revision."""

    def __init__(self, failures, **kw):
        super().__init__(**kw)
        self.failures = failures

    def set_traffic(self, service, targets):
        if targets == [Target(PREV, 100)] and self.failures > 0:
            self.failures -= 1
            raise RuntimeError("503 backend error")
        super().set_traffic(service, targets)


def test_a_transient_failure_during_rollback_is_retried():
    fake, _, ro, _ = make(cand=(BAD,), fake=RollbackFlaky(2, serving=PREV, new_revision=NEW))
    r = run(fake, ro)
    assert r.status == "rolled_back" and fake.traffic == (Target(PREV, 100),)


def test_a_rollback_that_cannot_complete_is_reported_loudly_and_never_as_success():
    fake, _, ro, _ = make(cand=(BAD,), fake=RollbackFlaky(99, serving=PREV, new_revision=NEW))
    r = run(fake, ro)
    assert r.status == "rollback_failed" and "intervene manually" in r.reason
    assert r.final_traffic and r.final_traffic[0].revision == NEW, "the report shows where traffic really is"


class Liar(FakeCloudRun):
    """Accepts a traffic change and silently does not apply it."""

    def set_traffic(self, service, targets):
        if any(t.revision == NEW and t.percent == 10 for t in targets):
            return
        super().set_traffic(service, targets)


def test_a_traffic_change_that_did_not_take_effect_is_caught_by_reading_it_back():
    fake, _, ro, _ = make(fake=Liar(serving=PREV, new_revision=NEW))
    r = run(fake, ro)
    assert r.status == "rolled_back" and "did not take effect" in r.reason


# --- the controller's own guards and the fake's -------------------------------------------------------------------


@pytest.mark.parametrize("steps", [(), (50,), (10, 100, 50), (0, 100), (10, 10, 100), (10, 90)])
def test_nonsense_step_lists_are_rejected(steps):
    with pytest.raises(ValueError):
        Rollout(FakeCloudRun(serving=PREV), StaticMetrics({}), lambda u: (True, ""), steps=steps)


def test_the_fake_refuses_what_cloud_run_would_refuse():
    fake = FakeCloudRun(serving=PREV)
    with pytest.raises(CloudRunRefused, match="sum to 100"):
        fake.set_traffic("orders", [Target(PREV, 60)])
    with pytest.raises(CloudRunRefused, match="does not exist"):
        fake.set_traffic("orders", [Target("nope", 100)])
    with pytest.raises(CloudRunRefused, match="at least one"):
        fake.set_traffic("orders", [])
    with pytest.raises(CloudRunRefused, match="whole numbers"):
        fake.set_traffic("orders", [Target(PREV, 150), Target(PREV, -50)])
