"""Kiểm tra failover với snapshot thật và HTTP giả lập, không chạm state lab."""
import json
import pathlib

import httpx
import pytest

from dr import failover as fo
from state.seed_vectors import seed


@pytest.mark.parametrize("ready", [True, False])
def test_restore_and_cutover_only_when_ready(tmp_path, monkeypatch, ready):
    monkeypatch.chdir(tmp_path)
    seed("a", 2, 0)
    weights = pathlib.Path("state/region-a/weights")
    (weights / "model.bin").write_bytes(b"model")
    (weights / "VERSION").write_text("test-version\n")
    fo.snapshot.put("a", "fs")
    seed("b", 0, 0)
    pathlib.Path("edge").mkdir()
    active = pathlib.Path("edge/active_region")
    active.write_text("a")

    def get(url, **kwargs):
        if url.endswith("/v1/state"):
            body = {"region": "b", "pool_state": "warm", "weights": False,
                    "count": 0}
            status = 200
        else:
            assert active.read_text() == "a"
            assert pathlib.Path("state/region-b/pool_state").read_text() == "full"
            assert pathlib.Path("state/region-b/weights/model.bin").read_bytes() == b"model"
            body = {"pool_state": "full", "vectors": {"count": 2}}
            status = 200 if ready else 503
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(fo.httpx, "get", get)
    result = fo.failover("b", "fs", wait=0.02)
    events = [json.loads(line) for line in fo.LOG.read_text().splitlines()]
    assert result["ok"] is ready
    assert active.read_text() == ("b" if ready else "a")
    assert [event["step"] for event in events] == [
        "1_verify_target", "2_restore_snapshot", "3_scale_pool", "4_wait_ready",
    ] + (["5_dns_cutover"] if ready else [])
    assert events[1]["rpo_seconds"] == 0
    assert events[1]["docs_lost"] == 0
    assert events[1]["embed_model_version"] == "test-version"
    assert events[-1]["ok"] is ready
