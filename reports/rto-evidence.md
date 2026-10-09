# RTO/RPO Evidence — Lab 23 (SỐ THẬT từ drill chạy ngày 2026-10-09)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T03:17:02` | chaos kill region-a (netblock) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.0s` | dòng `ok:false` đầu sau t_outage | `reports/drill-1-nodr.jsonl:37` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | 11 request failed, không request nào OK sau kill | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill`, ISO `2026-10-09T03:42:00` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.2s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:122` |
| Health check phát hiện | +16.7s | `to:UNHEALTHY, region:a` sau 3 lần fail | `reports/health-events.jsonl:3` |
| Snapshot restore xong | +21.9s | `step:2_restore_snapshot`, rpo 8.0s, 4 doc | `reports/failover-events.jsonl:7` |
| Region phụ ready | +28.1s | `step:4_wait_ready`, `waited_s:6.12` | `reports/failover-events.jsonl:9` |
| DNS cutover | +28.1s | `step:5_dns_cutover`, active_region=b | `reports/failover-events.jsonl:10` |
| **RTO đo được** | **+30.4s** | dòng `ok:true` đầu, `served_by:b` | `reports/drill-2-withdr.jsonl:137` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO — Inference API | `30.4s` | 300s | **PASS** |
| RPO — Vector DB | `8.0s` / `4` doc | 300s | **PASS** |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm bằng cách nào |
|---|---|---|---|
| Phát hiện outage | 21.9s (chặn dưới tự động: detect floor 15.0s) | runbook confirm 3 probe; floor = `interval_s × threshold` = 5×3 ghi ở `reports/health-events.jsonl:3` | Hạ interval/threshold — trả giá flapping |
| Snapshot restore | ~0.0s | `2_restore_snapshot`→`3_scale_pool` trong `reports/failover-events.jsonl:7` | Đã gần miễn phí (fs cục bộ) |
| GPU pool warm-up | 6.12s | `waited_s:6.12` ở `reports/failover-events.jsonl:9` | Pre-stage weights / giữ pool B ở `full` — trả giá GPU |
| DNS/LB TTL cache | 2.3s | t_recovered − t_cutover = 30.4 − 28.1 | Giảm TTL — trả giá tải DNS |

Tổng: 21.9 + ~0.0 + 6.12 + 2.3 ≈ **30.4s** = RTO đo được.
