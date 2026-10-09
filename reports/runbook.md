# Runbook 1 trang — Region chính (A) down

Chạy được lúc 3h sáng bởi người KHÔNG viết code này. Mỗi bước: lệnh copy-paste được +
cách biết bước đó xong. Chạy từ thư mục gốc của repo. Mặc định runbook hỏi confirm
`y/N` (bán tự động, chống flap); thêm `--auto` chỉ khi đã chắc chắn.

## Điều kiện kích hoạt

Bước 1 phải xác nhận region A fail 3 lần liên tiếp (đừng tin 1 lần fail — có thể chỉ là
lag mạng thoáng qua). Nếu `dr/health_checker.py` đang chạy, tín hiệu bổ sung là dòng
`to:UNHEALTHY region:a` trong `reports/health-events.jsonl`.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `"a":{"alive":false}` lặp lại 3 lần cách nhau 5s; b vẫn `alive:true`. Nếu b cũng chết → KHÔNG failover, leo thang ngay (double outage) | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | dòng `step:2 name:thong_bao_incident` có `incident_ts` + `notify_lag_s` trong `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | tự động trong runbook; chạy tay nếu cần: `python3 dr/failover.py --target b --backend fs` | dòng `step:2_restore_snapshot` có `rpo_seconds` + `docs_lost` + `embed_model_version` trong `reports/failover-events.jsonl`. Lỗi "chưa từng có put" → chạy `python3 state/snapshot.py put --region a --backend fs` trước (chỉ được nếu A chưa chết hẳn) | on-call |
| 4 | Scale pool warm→full + chờ ready | tự động (bước `3_scale_pool` + `4_wait_ready` của failover) | dòng `step:4_wait_ready ready:true` trong `reports/failover-events.jsonl` VÀ `curl localhost:8002/readyz` trả HTTP 200. **Timeout → DỪNG, tuyệt đối không cutover** | on-call |
| 5 | DNS/LB cutover | tự động (bước `5_dns_cutover`); chạy tay: `python3 -c "open('edge/active_region','w').write('b')"` | `curl localhost:8080/edge/state` cho `"active_region":"b"` (chờ tối đa 5s TTL) | on-call |
| 6 | Verify golden signals | tự động: 10 request thật vào region B | dòng `step:6 verify_golden_signals` có `p95_latency_ms < 100` và `error_rate = 0`. Trong drill 2026-10-09: p95 = 2.1ms, error = 0. Nếu error_rate > 0 → ROLLBACK | on-call + SRE lead |
| 7 | Đo RTO + viết postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` ra số (`PASS` nếu ≤ 300s); điền `reports/postmortem.md` trong 24h | SRE lead |

## Rollback (failover ngược về A)

Chỉ thực hiện khi ĐỦ CẢ 3 điều kiện:
1. Region A đã `ready:true` thật: `curl localhost:8001/readyz` trả 200;
2. Dữ liệu ghi vào B trong thời gian outage đã được replicate ngược
   (`python3 state/snapshot.py put --region b --backend fs` và kiểm tra MANIFEST);
3. **SRE lead là người quyết định** — không ai tự động bấm rollback.

Lệnh: `python3 dr/failover.py --target a --backend fs` (đủ 5 bước, gồm chờ A ready trước
khi cutover). Quy tắc chống flap: tối thiểu 30 phút giữa hai lần đổi vùng; cấm failover
tự động 2 chiều không có circuit breaker (bài học §4 Anti-Patterns).

## Cứu sinh khi mọi thứ hỏng

`python3 chaos/kill_region.py restore --region a --backend bare` luôn chạy được
(SIGCONT/SIGKILL recovery), sau đó `bash scripts/up_bare.sh` để dựng lại cả stack.