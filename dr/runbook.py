"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước vào log riêng của runbook."""
    rec = {"ts": time.time(),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))


def confirm(auto: bool, msg: str) -> bool:
    """Mặc định cần operator xác nhận; auto dành cho drill/CI."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Xác nhận outage, thực hiện failover một lần và kiểm tra kết quả."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("Primary và target phải là hai region khác nhau")
    if backend not in {"fs", "minio"}:
        raise ValueError("Backend không hợp lệ")
    started = time.monotonic()

    for attempt in range(3):
        checked_at = time.monotonic()
        ready, reason = hc.probe(primary, 2.0)
        try:
            response = httpx.get(f"{URL[target]}/healthz", timeout=1.5)
            target_alive = response.status_code == 200
        except httpx.RequestError:
            target_alive = False
        if ready or not target_alive:
            error = "primary_ready" if ready else "target_not_alive"
            step(1, "xac_nhan_outage", ok=False, primary=primary,
                 target=target, error=error)
            return {"ok": False, "error": error}
        if attempt < 2:
            time.sleep(max(0.0, 5.0 - (time.monotonic() - checked_at)))
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target,
         consecutive_fails=3, interval_s=5.0, reason=reason)

    if not confirm(auto, f"Chuyển traffic từ {primary} sang {target}?"):
        return {"ok": False, "error": "operator_declined"}

    t_outage = None
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_log.exists():
        for line in reversed(chaos_log.read_text().splitlines()):
            event = json.loads(line)
            if event.get("region") == primary and event.get("action") in {"kill", "restore"}:
                if event["action"] == "kill":
                    t_outage = event["ts"]
                break
    t_incident = time.time()
    step(2, "thong_bao_incident", ok=True, primary=primary, target=target,
         t_outage=t_outage, t_incident=t_incident,
         notification_delay_s=None if t_outage is None else t_incident - t_outage)

    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=result.get("ok", False), result=result)
    if not result.get("ok"):
        return result

    state = result["state"]
    replica_ok = state.get("weights") is True and state.get("count", 0) > 0
    step(4, "verify_state_replica", ok=replica_ok, state=state)
    cutover_ok = result.get("cutover") is True
    step(5, "dns_cutover", ok=cutover_ok, target=target)

    latencies = []
    errors = 0
    for _ in range(10):
        request_started = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            if response.status_code != 200 or response.json().get("region") != target:
                errors += 1
        except (httpx.RequestError, ValueError):
            errors += 1
        latencies.append((time.monotonic() - request_started) * 1000)
    # Nearest-rank p95 của 10 mẫu là mẫu lớn nhất.
    p95_ms = round(max(latencies), 1)
    error_rate = errors / 10
    step(6, "verify_golden_signals", ok=errors == 0, requests=10,
         errors=errors, p95_latency_ms=p95_ms, error_rate=error_rate)

    summary = {"ok": replica_ok and cutover_ok and errors == 0,
               "primary": primary, "target": target,
               "elapsed_s": round(time.monotonic() - started, 2),
               "p95_latency_ms": p95_ms, "error_rate": error_rate,
               "measure_command": "python tools/measure_rto.py --loadgen "
                                  "reports/drill-2-withdr.jsonl --target-rto 300"}
    step(7, "post_incident", **summary)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
