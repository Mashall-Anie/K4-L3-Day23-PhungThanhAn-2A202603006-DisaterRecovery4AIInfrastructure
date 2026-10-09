# Postmortem — DR Drill Lab 23 (Blameless)

**Incident:** Region A (inference primary) mất khả năng phục vụ do netblock (mô phỏng mất mạng).
**Ngày:** 2026-10-09 · **Severity:** SEV-1 (toàn bộ traffic inference gián đoạn).
**Kết quả:** phục hồi tự động sau 30.4s, mất 4 document. Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T03:42:00 | outage bắt đầu — chaos kill region-a (netblock), t_outage = mốc 0 | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T03:42:00 | user đầu tiên bị ảnh hưởng (+0.2s, ReadTimeout qua edge) | `reports/drill-2-withdr.jsonl:122` |
| 2026-10-09T03:42:16 | health checker tự động alert: region-a UNHEALTHY (+16.7s, detect floor 5s×3) | `reports/health-events.jsonl:3` |
| 2026-10-09T03:42:22 | runbook xác nhận outage (3/3 probe fail) + mở incident, notify_lag 21.9s | `reports/runbook-run.jsonl` |
| 2026-10-09T03:42:28 | cutover hoàn tất — region-b ready sau warm-up 6.12s, active_region=b | `reports/failover-events.jsonl:10` |
| 2026-10-09T03:42:30 | resolved — request đầu tiên OK từ region-b, RTO 30.4s | `reports/drill-2-withdr.jsonl:137` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: **30.4s** · gap: **dư 269.6s — PASS** (`reports/measure-drill-2.json`)
- RPO mục tiêu: 300s · đo được: **8.0s** (4 doc bị mất) · gap: **dư 292s — PASS**
- **Bước tốn nhiều giây nhất:** phát hiện outage — runbook confirm hết 21.9s (3 probe,
  mỗi probe treo đủ timeout vì netblock + 2s nghỉ giữa các lần), chặn dưới tự động là
  detect floor 15.0s. Riêng khâu phát hiện chiếm ~72% RTO.
- So sánh baseline: cùng outage này mà không có DR thì verdict là `NO_RECOVERY`
  (`reports/measure-drill-1.json`) — gap giữa "có DR" và "không DR" là vô cực.

## 3. Root cause (5 whys)

## 3. Root cause (5 whys)

1. **Vì sao user thấy lỗi?** Vì region A không trả lời request (bị SIGSTOP/netblock).
2. **Vì sao traffic vẫn đổ vào A hơn 28 giây?** Vì DNS/edge chỉ trỏ sang B khi có ai
   đổi `edge/active_region` — trước drill này không tồn tại cơ chế phát hiện + cutover.
3. **Vì sao không phát hiện sớm hơn?** Vì hệ thống chưa có health checker; và kể cả khi
   có, detect floor = interval × threshold = 15s là giá phải trả để chống flapping.
4. **Vì sao region B không serve được ngay khi trỏ sang?** Vì B khởi đầu rỗng:
   `pool_state=warm`, không weights, vector DB `count=0` — không có replication nào đưa state sang.
5. **Vì sao B rỗng mà vẫn được coi là "region dự phòng"?** Vì quy trình provision chỉ
   copy compute, không copy STATE (vector DB + weights + pool config).
   **Root cause thật:** *"process sống ≠ region serve được"* — và DR chưa từng được diễn
   tập nên gap này không ai nhìn thấy trước drill.

Nếu đây là outage thật, bước runbook nào sẽ thất bại? Bước 3 (restore) — nếu
`state/replicate.py` chưa từng chạy thì `snapshot.get` chết vì không có MANIFEST.
Replication phải là daemon chạy nền vĩnh viễn, không phải thứ "bật lên khi drill".

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Chạy `state/replicate.py` như daemon nền (mỗi 30s), có giám sát, để snapshot luôn tồn tại | SRE team | 2026-10-16 | RPO: giữ ≤ 30s mọi lúc (thay vì phụ thuộc ai đó nhớ bật) |
| 2 | Hạ interval health check 5s→2s (giữ threshold 3): detect floor 15s→6s, vẫn còn confirm của operator chống flap | SRE team | 2026-10-23 | RTO: −9s đến −12s ở khâu phát hiện |
| 3 | Pre-stage model weights ở region B (hoặc giữ pool B ở `full` ngoài giờ cao điểm) để bỏ warm-up | Platform team | 2026-10-30 | RTO: −6.12s (toàn bộ `waited_s` của 4_wait_ready) |
| 4 | Thêm circuit breaker + cooldown 30 phút cho failover ngược (B→A), chống flap 2 chiều | SRE team | 2026-11-06 | Không giảm RTO — ngăn RTO tăng do flap |
| 5 | Diễn tập drill hằng tháng, đo lại RTO/RPO từ log, cập nhật evidence | On-call lead | 2026-11-09 | Giữ con số RTO luôn là số đo thật, không phải số trên giấy |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15s** detect floor (`reports/health-events.jsonl:3`). Chiếm ~49% RTO
   (15/30.4) nếu so với chặn dưới; thực tế khâu phát hiện (gồm cả runbook confirm)
   chiếm ~72% vì operator mất 21.9s mới xác nhận xong. Phát hiện là thành phần áp đảo.
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
   Detect floor còn 1s × 3 = 3s → RTO lý thuyết giảm ~12–13s (còn ~17–18s). Trả giá:
   (a) flapping — một đợt lag mạng 3 giây cũng đủ kích hoạt failover nhầm;
   (b) tải probe ×5 lên cả 2 region; (c) mọi nguồn nhiễu đều có xác suất false-positive cao hơn.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` nghĩa là gì với khách hàng?**
   `docs_lost=4` đo tại thời điểm restore = số doc ingest vào A sau snapshot cuối — với
   replicate mỗi 30s, worst case chỉ ~15 doc (≤30s dữ liệu), khách mất nhiều nhất nửa
   phút ticket gần nhất. Nhưng trong outage 6 giờ, mọi ingest mới đổ vào B; nếu không
   replicate ngược B→A trước khi rollback thì mất TOÀN BỘ 6 giờ dữ liệu mới — RPO của
   chiều về cũng phải được đo, không chỉ chiều đi.