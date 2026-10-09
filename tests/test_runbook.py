"""Runbook tests use isolated logs and simulated HTTP, without live services."""
import json
import time
from unittest.mock import Mock

import httpx
import pytest

from dr import runbook as rb


@pytest.mark.parametrize("answer, expected", [("y", True), ("Y", True), ("", False), ("n", False)])
def test_confirmation_defaults_to_no(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert rb.confirm(False, "Proceed?") is expected


def test_auto_confirmation_does_not_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unexpected prompt"))
    assert rb.confirm(True, "Proceed?")


@pytest.mark.parametrize("scenario", ["success", "primary_ready", "target_down",
                                      "declined", "failover_failed", "inference_failed"])
def test_runbook_flow(tmp_path, monkeypatch, scenario):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rb.time, "sleep", lambda _: None)
    probe = Mock(return_value=(scenario == "primary_ready", "HTTP 503"))
    monkeypatch.setattr(rb.hc, "probe", probe)
    monkeypatch.setattr(rb, "confirm", lambda *args: scenario != "declined")
    result = {"ok": scenario != "failover_failed", "cutover": True,
              "state": {"count": 200, "weights": True, "pool_state": "full"}}
    failover = Mock(return_value=result)
    monkeypatch.setattr(rb.fo, "failover", failover)
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        status = 200
        if url.endswith("/healthz") and scenario == "target_down":
            status = 503
        if url.endswith("/v1/infer") and scenario == "inference_failed":
            raise httpx.ReadTimeout("Simulated timeout")
        return httpx.Response(status, json={"region": "b"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(rb.httpx, "get", get)
    chaos = tmp_path / "chaos/chaos-events.jsonl"
    chaos.parent.mkdir()
    outage = time.time() - 20
    chaos.write_text(json.dumps({"action": "kill", "region": "a", "ts": outage}) + "\n")

    summary = rb.run("a", "b", "fs", auto=True)
    events = [json.loads(line) for line in rb.LOG.read_text().splitlines()]
    if scenario in {"primary_ready", "target_down", "declined"}:
        failover.assert_not_called()
        assert not summary["ok"]
        assert [event["step"] for event in events] == [1]
        return

    failover.assert_called_once_with("b", "fs", wait=60.0)
    assert probe.call_count == 3
    assert events[1]["t_outage"] == outage
    assert events[1]["t_incident"] > outage
    if scenario == "failover_failed":
        assert not summary["ok"]
        assert [event["step"] for event in events] == [1, 2, 3]
        assert not any(url.endswith("/v1/infer") for url in requests)
        return

    assert [event["step"] for event in events] == list(range(1, 8))
    assert sum(url.endswith("/v1/infer") for url in requests) == 10
    assert summary["ok"] is (scenario == "success")
    assert summary["error_rate"] == (0 if scenario == "success" else 1)
    assert summary["p95_latency_ms"] >= 0
