# Runbook — Region A down, chuyển sang Region B

Chạy từ thư mục gốc dự án trong WSL, với `source .venv/bin/activate`.
Dịch vụ bare mode phải đang chạy; tạo snapshot trước sự cố bằng
`python state/snapshot.py put --region a --backend fs` hoặc replication định kỳ.
Không chạy `make seed` trong lúc xử lý incident.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status --backend bare` | A không ready, B còn alive; runbook kiểm tra A lỗi 3 lần liên tiếp | On-call |
| 2 | Mở incident và xác nhận | `python dr/runbook.py --primary a --target b --backend fs` | Nhập `y`; log `thong_bao_incident` có timestamp incident | On-call |
| 3 | Restore và scale pool | `tail -n 5 reports/failover-events.jsonl` | Runbook ghi `2_restore_snapshot` và `3_scale_pool` với `ok:true`, RPO và embedding version | Runbook; on-call theo dõi |
| 4 | Verify state replica | `curl --max-time 5 http://localhost:8002/v1/state` | `weights:true`, `count>0`, `pool_state:full`; log `4_wait_ready` thành công | Runbook; on-call kiểm tra |
| 5 | Xác nhận cutover | `curl --max-time 5 http://localhost:8080/edge/state` | `active_region:b` sau cache TTL; log có `5_dns_cutover` | Runbook; on-call kiểm tra |
| 6 | Golden signals | `tail -n 2 reports/runbook-run.jsonl` | Bước 6 ghi 10 request, `error_rate:0`, p95 đo được; kiểm tra thêm `curl --max-time 5 http://localhost:8080/v1/infer` phục vụ từ B | Runbook; on-call đánh giá |
| 7 | Đo RTO và postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Traffic đã kết thúc; `valid:true`, warnings rỗng, `PASS`, phục hồi bởi B | On-call; incident lead duyệt |

Chỉ chạy runbook ở bước 2 một lần. Các bước 3–6 quan sát tiến trình đó,
không gọi lại failover hoặc restore thủ công. `--auto` dành cho drill/CI.
Nếu trả `ok:false`, đọc lỗi trước khi thử lại; không tự đổi active_region.
P95 được báo cáo để đánh giá; lab chưa cấu hình ngưỡng latency.
Đo RTO từ traffic, không dùng elapsed_s của runbook thay thế.

## Rollback có phê duyệt

Incident lead quyết định trả traffic về A khi B không phục vụ ổn định.
A phải ready, có weights và dữ liệu phù hợp; dữ liệu phát sinh ở B phải được đồng bộ
trước khi chuyển. Nếu cả hai vùng không ready, giữ incident mở; không đổi qua lại.

Với SIGSTOP trong lab, on-call khôi phục A bằng
`python chaos/kill_region.py restore --region a --backend bare`, rồi kiểm tra
`curl --max-time 5 http://localhost:8001/readyz` và trạng thái dữ liệu hai vùng.
Sau khi incident lead chấp thuận dữ liệu tại A, chạy `printf a > edge/active_region`,
chờ TTL tối đa 5 giây mặc định và kiểm tra
`curl --max-time 5 http://localhost:8080/v1/infer` trả `region:a`, HTTP 200.
Đây là rollback sau drill, không thay thế failover trong thời gian đo RTO.
Ghi quyết định và thời điểm rollback vào postmortem.
