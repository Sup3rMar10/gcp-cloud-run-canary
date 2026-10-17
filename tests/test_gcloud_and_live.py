import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rollout import DeployError, GcloudCloudRun, Target, monitoring_filter
from rollout.live import http_smoke

DOC = {"status": {"latestReadyRevisionName": "orders-00002-abc", "traffic": [
    {"revisionName": "orders-00001-aaa", "percent": 100},
    {"revisionName": "orders-00002-abc", "percent": 0, "tag": "candidate", "url": "https://candidate---orders-x-uc.a.run.app"}]}}


class Runner:
    def __init__(self, doc=DOC, fail_deploy=False, no_service=False):
        self.cmds, self.doc, self.fail_deploy, self.no_service = [], doc, fail_deploy, no_service

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if "describe" in cmd:
            if self.no_service:
                raise subprocess.CalledProcessError(1, cmd, stderr="not found")
            return json.dumps(self.doc)
        if "deploy" in cmd and self.fail_deploy:
            raise subprocess.CalledProcessError(1, cmd, stderr="Revision failed to start")
        return ""


def client(**kw):
    r = Runner(**kw)
    return GcloudCloudRun("acme-prod", "us-central1", runner=r), r


# --- the commands it builds -------------------------------------------------------------------------------------


def test_every_command_names_the_project_and_region():
    c, r = client()
    c.describe("orders")
    c.deploy("orders", "img:2", "candidate")
    c.set_traffic("orders", [Target("a", 10), Target("b", 90)])
    c.remove_tag("orders", "candidate")
    for cmd in r.cmds:
        assert cmd[:2] == ["gcloud", "run"]
        assert cmd[cmd.index("--project") + 1] == "acme-prod" and cmd[cmd.index("--region") + 1] == "us-central1"


def test_a_deploy_creates_a_revision_with_no_traffic_and_a_tag():
    c, r = client()
    rev = c.deploy("orders", "img:2", "candidate")
    deploy = next(cmd for cmd in r.cmds if "deploy" in cmd)
    assert "--no-traffic" in deploy and deploy[deploy.index("--tag") + 1] == "candidate"
    assert deploy[deploy.index("--image") + 1] == "img:2"
    assert rev == "orders-00002-abc", "the revision is found by its tag, not guessed"


def test_a_traffic_split_is_expressed_with_to_revisions():
    c, r = client()
    c.set_traffic("orders", [Target("orders-00002-abc", 10), Target("orders-00001-aaa", 90)])
    cmd = r.cmds[-1]
    assert "update-traffic" in cmd and "orders" in cmd
    assert cmd[cmd.index("--to-revisions") + 1] == "orders-00002-abc=10,orders-00001-aaa=90"


def test_removing_a_tag_uses_remove_tags():
    c, r = client()
    c.remove_tag("orders", "candidate")
    assert r.cmds[-1][r.cmds[-1].index("--remove-tags") + 1] == "candidate"


# --- reading the service -------------------------------------------------------------------------------------------


def test_describe_keeps_only_revisions_that_carry_traffic():
    c, _ = client()
    state = c.describe("orders")
    assert state.traffic == (Target("orders-00001-aaa", 100),)
    assert "orders-00002-abc" in state.ready_revisions


def test_a_service_that_does_not_exist_yet_describes_as_empty():
    c, _ = client(no_service=True)
    assert c.describe("orders").traffic == ()


def test_a_failed_deploy_becomes_a_deploy_error_with_the_reason():
    c, _ = client(fail_deploy=True)
    with pytest.raises(DeployError, match="failed to start"):
        c.deploy("orders", "img:2", "candidate")


def test_a_deploy_whose_tag_cannot_be_found_is_an_error_not_a_guess():
    c, _ = client(doc={"status": {"traffic": [{"revisionName": "orders-00001-aaa", "percent": 100}]}})
    with pytest.raises(DeployError, match="no revision carries the tag"):
        c.deploy("orders", "img:2", "candidate")


def test_the_tag_url_is_read_from_the_service():
    c, _ = client()
    assert c.tag_url("orders", "candidate") == "https://candidate---orders-x-uc.a.run.app"
    with pytest.raises(DeployError):
        c.tag_url("orders", "other")


# --- metrics filter ------------------------------------------------------------------------------------------------


def test_the_monitoring_filter_selects_one_revision_of_one_service():
    f = monitoring_filter("orders", "orders-00002-abc")
    for part in ('metric.type = "run.googleapis.com/request_count"', 'resource.type = "cloud_run_revision"',
                 'resource.labels.service_name = "orders"', 'resource.labels.revision_name = "orders-00002-abc"'):
        assert part in f
    assert "response_code_class" not in f
    assert 'metric.labels.response_code_class = "5xx"' in monitoring_filter("orders", "r", "5xx")


# --- the HTTP smoke test, against a real local server --------------------------------------------------------------


@pytest.fixture()
def server():
    state = {"status": 200, "hits": 0, "auth": None, "fail_first": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["hits"] += 1
            state["auth"] = self.headers.get("Authorization")
            code = 503 if state["fail_first"] > 0 else state["status"]
            state["fail_first"] = max(0, state["fail_first"] - 1)
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def test_smoke_passes_on_200(server):
    url, state = server
    assert http_smoke(url) == (True, "")


def test_smoke_fails_with_the_status_when_the_service_keeps_returning_503(server):
    url, state = server
    state["status"] = 503
    ok, detail = http_smoke(url, attempts=3, pause=0, sleep=lambda s: None)
    assert not ok and "503" in detail and state["hits"] == 3


def test_smoke_tolerates_a_slow_start(server):
    url, state = server
    state["fail_first"] = 2
    assert http_smoke(url, attempts=5, pause=0, sleep=lambda s: None)[0] and state["hits"] == 3


def test_smoke_sends_the_bearer_token_when_given_one(server):
    url, state = server
    http_smoke(url, token="abc")
    assert state["auth"] == "Bearer abc"


def test_smoke_fails_cleanly_when_nothing_is_listening():
    ok, detail = http_smoke("http://127.0.0.1:9", attempts=2, pause=0, timeout=1, sleep=lambda s: None)
    assert not ok and "failed" in detail
