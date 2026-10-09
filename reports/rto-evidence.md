# RTO/RPO Evidence — Lab 23

Số đo Drill 2 ngày 2026-10-09 qua runbook --auto, bare mock, backend fs,
có replication định kỳ và dữ liệu ingest. Mỗi evidence trỏ về dòng log thật.

## 1. Drill 1 — không DR

| Chỉ số | Giá trị | Evidence |
|---|---|---|
| Outage | 2026-10-09T05:23:55 UTC | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu | +0.2s | `reports/drill-1-nodr.jsonl:12` |
| Phục hồi trong cửa sổ traffic | Không có; NO_RECOVERY | `reports/measure-drill-1.json` |

## 2. Drill 2 — phục hồi qua runbook

| Mốc | Từ outage | Evidence |
|---|---|---|
| Outage | 0.0s | `chaos/chaos-events.jsonl:7` |
| Request lỗi đầu tiên | 2.0s | `reports/drill-2-withdr.jsonl:61` |
| Runbook xác nhận outage | 13.3s | `reports/runbook-run.jsonl:8` |
| Mở incident (--auto) | 13.3s | `reports/runbook-run.jsonl:9` |
| Verify target | 13.4s | `reports/failover-events.jsonl:17` |
| Restore xong | 13.6s | `reports/failover-events.jsonl:18` |
| Health checker phát hiện | 14.4s | `reports/health-events.jsonl:2` |
| Region B ready | 20.3s | `reports/failover-events.jsonl:20` |
| DNS cutover | 20.3s | `reports/failover-events.jsonl:21` |
| Golden signals | 21.4s | `reports/runbook-run.jsonl:13` |
| Request đầu tiên thành công từ B | 25.1s | `reports/drill-2-withdr.jsonl:72` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO — Inference API | 25.1s | 300s | PASS |
| RPO — Vector DB | 14.0s / 7 doc | 300s | Đạt mục tiêu theo giây |

Có 11 request lỗi, valid=true, warnings rỗng; xem `reports/measure-drill-2.json`.
RPO và embedding version tại `reports/failover-events.jsonl:18`.
B phục hồi 435 tài liệu, có weights và pool full tại `reports/runbook-run.jsonl:11`.
Replication cấu hình mỗi 30.0 giây; snapshot cuối trước outage tại `reports/replication.jsonl:12`.
Golden signals gồm 10 request trực tiếp B, p95=157.4ms, error_rate=0.0;
bằng chứng `reports/runbook-run.jsonl:13`. Các request này không đo đường qua edge;
RTO sử dụng traffic qua edge.

## 3. Phân tích RTO và phần thời gian chồng lấp

Health-check interval=5s, threshold=3, tích cấu hình detect_floor_s=15s,
tại `reports/health-events.jsonl:2` và `reports/measure-drill-2.json`.
Detection thực tế là 14.408s. Ba lần lỗi có khoảng hai interval giữa chúng;
thời điểm outage trong chu kỳ và timeout làm số đo khác tích cấu hình.

| Khoảng quan sát | Giây | Cách tính |
|---|---|---|
| Verify→restore, gồm restore và đo RPO | 0.165s | step 2 − step 1 |
| Scale→ready, gồm GPU warm-up và polling | 6.705s | step 4 − step 3 |
| Cutover→request phục hồi, gồm DNS/LB TTL và nhịp traffic | 4.833s | traffic thành công − step 5 |

Runbook xác nhận outage riêng và bắt đầu restore trước khi health checker ghi alert.
Vì vậy không cộng tất cả thời lượng trên với detection: một phần đã diễn ra song song.
Bảng dưới phân bổ bốn thành phần trên trục thời gian để tránh tính hai lần:

| Thành phần đóng góp vào tổng | Giây | Giải thích |
|---|---|---|
| Health-check detection thực tế | 14.408s | Outage→alert; tích cấu hình 15s được ghi riêng ở trên |
| Restore sau alert | 0.000s | Restore hoàn tất trước alert nên đóng góp sau alert bằng 0; thời lượng quan sát vẫn là 0.165s |
| Phần GPU warm-up và hoàn tất cutover sau alert | 5.882s | Alert→cutover; phần warm-up trước alert nằm trong dòng detection |
| DNS/LB TTL và chờ request | 4.833s | Cutover→request thành công |
| Tổng | 25.122s, làm tròn 25.1s | Tổng bốn phần không chồng lấp |

Các hiệu timestamp là khoảng quan sát; log chưa có thời điểm bắt đầu restore hay waited_s.
Không xem các giá trị này là phép đo riêng tuyệt đối từng thao tác.
Test detection yêu cầu >=14s; lần mới đo 14.4s nên số đo đáp ứng điều kiện đó.
