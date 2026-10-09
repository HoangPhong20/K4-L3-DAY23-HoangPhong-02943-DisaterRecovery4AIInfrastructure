# Postmortem — DR Drill Lab 23

## 1. Timeline (UTC)

| Thời gian | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T16:54:26.115+00:00 | Outage | `chaos/chaos-events.jsonl:7` |
| 2026-10-09T16:54:28.098+00:00 | Request lỗi đầu tiên | `reports/drill-2-withdr.jsonl:61` |
| 2026-10-09T16:54:39.411+00:00 | Runbook xác nhận outage | `reports/runbook-run.jsonl:8` |
| 2026-10-09T16:54:39.420+00:00 | Mở incident (--auto) | `reports/runbook-run.jsonl:9` |
| 2026-10-09T16:54:39.521+00:00 | Verify target | `reports/failover-events.jsonl:17` |
| 2026-10-09T16:54:39.685+00:00 | Restore xong | `reports/failover-events.jsonl:18` |
| 2026-10-09T16:54:40.522+00:00 | Health checker phát hiện | `reports/health-events.jsonl:2` |
| 2026-10-09T16:54:46.396+00:00 | Region B ready | `reports/failover-events.jsonl:20` |
| 2026-10-09T16:54:46.404+00:00 | DNS cutover | `reports/failover-events.jsonl:21` |
| 2026-10-09T16:54:47.524+00:00 | Golden signals | `reports/runbook-run.jsonl:13` |
| 2026-10-09T16:54:51.237+00:00 | Request đầu tiên thành công từ B | `reports/drill-2-withdr.jsonl:72` |

Runbook dùng --auto trong drill, không có thao tác operator nhập y.
Incident được mở sau outage 13.305s.
Elapsed của runbook là 20.24s; không thay cho RTO tính từ traffic.

## 2. RTO/RPO và gap

- RTO mục tiêu 300s, đo 25.1s, còn dư 274.9s.
- RPO mục tiêu 300s, đo 14.0s, thiếu 7 tài liệu; còn dư 286.0s.
- Có 11 request lỗi; phục hồi từ B, valid=true, warnings rỗng.
- Thành phần lớn nhất trên trục thời gian là outage→health alert: 14.408s.
- Verify→restore mất 0.165s; scale→ready 6.705s; cutover→traffic thành công 4.833s.
- Detection, restore và warm-up có phần chồng lấp vì runbook xác nhận outage độc lập.
- Golden signals: 10 request tới B, p95 157.4ms, error rate 0.0; không phải latency qua edge.

Số đo tại `reports/measure-drill-2.json`; phân rã và path:line tại `reports/rto-evidence.md`.
Terminal từng báo database is locked trong ingest; lần drill gốc không có log ingest
để xác nhận thời gian chạy đầy đủ. Kiểm tra riêng sau drill đã xác minh script chạy
với duration=150, kết thúc thành công và không tái hiện lỗi lock; xem phần 6.
Gap còn lại là chưa xác định được tiến trình giữ lock ở lần lỗi cũ, và kiểm tra riêng
không thay thế bằng chứng thời gian ingest của lần drill gốc.

## 3. Root cause / 5 whys

1. User gặp lỗi vì vùng edge chọn không phản hồi.
2. Tiến trình A bị SIGSTOP để mô phỏng sự cố vùng.
3. Edge tiếp tục chọn A đến cutover vì chỉ đọc active_region và cache, không tự failover.
4. B có dữ liệu từ lần trước nhưng pool warm và snapshot cũ; cần restore snapshot mới và pool full.
5. Readiness kiểm tra dữ liệu, weights và warm-up; runbook chỉ cutover sau khi ready để tránh đưa user sang vùng chưa phục vụ được.

Replica gần nhất thiếu 7 tài liệu mới tại lúc restore; chu kỳ sao lưu không liên tục
nên có độ trễ dữ liệu. Nếu A mất đĩa vĩnh viễn, phép đo so SQLite A với B không còn
khả dụng; cần nguồn bền vững hoặc ledger ingest để định lượng mất dữ liệu.

## 4. Action items

| Action item | Owner | Deadline | Kết quả cần đo |
|---|---|---|---|
| Kiểm tra ingest 150s với replication và traffic; ghi log từ lần drill sau | Người triển khai | Kiểm tra riêng đã hoàn tất; bổ sung log ở drill tiếp theo | PASS trong kiểm tra riêng; xác định tiến trình giữ lock nếu lỗi tái diễn |
| Đo lại với replication thường xuyên hơn | On-call | Lần thử cải thiện tiếp theo | RPO và chi phí I/O; không cam kết mức giảm khi chưa đo |
| Thử giảm TTL hoặc giữ pool B full | Người triển khai | Sau khi lưu kết quả hiện tại | RTO mới và chi phí tài nguyên |

## 5. Câu hỏi phản tư

1. interval × threshold = 15s, bằng 59.8% RTO; detection thực tế 14.408s bằng 57.4%. Không cộng detection và restore chồng lấp.
2. Hạ interval từ 5s xuống 1s làm tích cấu hình giảm 12s, nhưng không đảm bảo RTO giảm đúng 12s vì timeout, lịch poll và warm-up. Tải probe tăng; lỗi ngắn dễ đạt ngưỡng hơn, cần giữ chống flapping và đo lại.
3. docs_lost=7 nghĩa là B thiếu 7 tài liệu so với database A tại lúc restore. Nếu A mất vĩnh viễn, khách hàng có thể mất nội dung và kết quả truy xuất tương ứng, trừ khi có nguồn dữ liệu khác để replay. Không suy ra mất dữ liệu trong 6 giờ chỉ từ phép đo này.

## 6. Xác minh ingest sau drill

Chạy nguyên script với `--region a --rate 0.5 --duration 150`, đồng thời replication
mỗi 30 giây và traffic inference 2 request/giây. Dữ liệu thử nằm riêng trên `/mnt/d`,
không thay đổi dữ liệu hoặc log drill dùng đo RTO/RPO. Dịch vụ thử bị SIGSTOP sau
20 giây và SIGCONT sau 40 giây để kiểm tra tác động khi serving ngừng phản hồi.
Traffic gọi trực tiếp vùng thử, không chạy lại toàn bộ edge/runbook/failover.

| Chỉ số | Kết quả | Evidence |
|---|---|---|
| Tham số ingest | rate=0.5, duration=150 | `reports/ingest-verification.log:3` |
| Exit code ingest | 0 | `reports/ingest-verification.log:5` |
| Exit code replication / traffic | 0 / 0 | `reports/ingest-verification.log:6`, `reports/ingest-verification.log:7` |
| Thời gian wall-clock, cùng loại đồng hồ script sử dụng | 151.613s | `reports/ingest-verification.log:9` |
| Thời gian monotonic quan sát | 146.343s | `reports/ingest-verification.log:8` |
| Tài liệu mới | 73 | `reports/ingest-verification.log:10` |
| Khoảng giữa tài liệu đầu và cuối | 149.311s | `reports/ingest-verification.log:11` |
| Chu kỳ replication | 5 | `reports/ingest-verification.log:12` |
| Traffic | 255 request, 6 lỗi trong lần thử có pause | `reports/ingest-verification.log:13`, `reports/ingest-verification.log:14` |
| SQLite integrity_check | ok | `reports/ingest-verification.log:15` |
| Kết quả kiểm tra | PASS | `reports/ingest-verification.log:16` |

Không yêu cầu đúng 75 tài liệu: tốc độ thực tế gồm thời gian ghi và chờ.
Script kiểm soát duration bằng time.time(); hai đồng hồ có số đo khác nhau trong
môi trường thử, nên báo cáo cả hai và không đồng nhất chúng. Ingest stdout/stderr
được giữ trong log, không có database is locked ở lần chạy này. Kết quả chứng minh
khả năng hoàn tất lần kiểm tra này, không bảo đảm mọi lần chạy sau đều không có lock.
