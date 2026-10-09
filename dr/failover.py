"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một sự kiện có timestamp vào log và stdout."""
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))


def failover(target: str, backend: str, wait: float) -> dict:
    """Chuẩn bị target và chỉ cutover sau khi readiness thành công."""
    if target not in URL or backend not in {"fs", "minio"} or wait <= 0:
        raise ValueError("Region, backend hoặc thời gian chờ không hợp lệ")

    step = "1_verify_target"
    try:
        response = httpx.get(f"{URL[target]}/v1/state", timeout=2.0)
        response.raise_for_status()
        state = response.json()
        emit(step=step, target=target, ok=True, state=state)

        step = "2_restore_snapshot"
        meta = snapshot.get(target, backend)
        primary = "a" if target == "b" else "b"
        metrics = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        emit(step=step, target=target, ok=True, **meta, **metrics)

        step = "3_scale_pool"
        pathlib.Path(f"state/region-{target}/pool_state").write_text("full")
        emit(step=step, target=target, ok=True, pool_state="full")

        step = "4_wait_ready"
        deadline = time.monotonic() + wait
        reason = "readiness_timeout"
        ready = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2.0, remaining))
                reason = f"HTTP {response.status_code}"
                if response.status_code == 200:
                    ready = response.json()
                    break
            except httpx.RequestError as exc:
                reason = type(exc).__name__
            time.sleep(max(0.0, min(0.5, deadline - time.monotonic())))
        if ready is None:
            raise TimeoutError(reason)
        state = {"region": target, "pool_state": ready["pool_state"],
                 "weights": True, **ready["vectors"]}
        emit(step=step, target=target, ok=True, state=ready)

        step = "5_dns_cutover"
        pathlib.Path("edge/active_region").write_text(target)
        emit(step=step, target=target, ok=True)
        return {"ok": True, "target": target, "cutover": True,
                "state": state, **meta, **metrics}
    except (Exception, SystemExit) as exc:
        emit(step=step, target=target, ok=False, error=str(exc))
        return {"ok": False, "target": target, "cutover": False,
                "step": step, "error": str(exc)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
