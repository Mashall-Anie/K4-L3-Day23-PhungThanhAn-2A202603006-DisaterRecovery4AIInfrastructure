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

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG — timeline của postmortem."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True (CI/grading); ngược lại HỎI người vận hành y/N (bán tự động)."""
    if auto:
        return True
    ans = input(f"{msg} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def probe_ready(region: str, timeout: float = 1.5) -> bool:
    """1 lần probe /readyz — True chỉ khi HTTP 200."""
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=timeout).status_code == 200
    except Exception:
        return False


def last_outage_ts() -> float | None:
    """t_outage = ts của sự kiện kill gần nhất trong chaos-events.jsonl."""
    p = pathlib.Path("chaos/chaos-events.jsonl")
    if not p.exists():
        return None
    last = None
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") == "kill":
            last = e
    return last["ts"] if last else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước incident response. Failover chỉ được gọi MỘT LẦN ở bước 3."""
    summary = {"ok": False, "primary": primary, "target": target, "auto": auto}

    # ---- 1. xac_nhan_outage: probe NHIỀU LẦN, đừng tin 1 lần fail ----
    probes = []
    for i in range(3):
        probes.append({"probe": i + 1, "primary_ready": probe_ready(primary),
                       "target_ready": probe_ready(target)})
        if i < 2:
            time.sleep(2.0)
    primary_down = all(not p["primary_ready"] for p in probes)
    step(1, "xac_nhan_outage", probes=probes, primary_down=primary_down)
    if not primary_down:
        step(1, "xac_nhan_outage_abort",
             note=f"region-{primary} van tra loi /readyz -> KHONG failover (chong flapping)")
        summary["error"] = f"region-{primary} van ready sau 3 lan probe"
        return summary
    if not confirm(auto, f"Region {primary} DOWN (3/3 probe fail). "
                         f"Failover sang region {target}?"):
        step(1, "operator_declined", note="operator tu choi failover")
        summary["error"] = "operator declined"
        return summary

    # ---- 2. thong_bao_incident: bấm giờ RTO từ đây ----
    t_outage = last_outage_ts()
    incident_ts = time.time()
    step(2, "thong_bao_incident", incident_ts=incident_ts, t_outage=t_outage,
         notify_lag_s=None if t_outage is None else round(incident_ts - t_outage, 2),
         note="operator biet tin — moc bat dau dong ho RTO phia operator")
    summary["incident_ts"] = incident_ts

    # ---- 3. scale_gpu_pool: gọi failover() MỘT LẦN DUY NHẤT ----
    fo_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_ok=fo_result.get("ok"),
         steps_done=fo_result.get("steps"), rpo_seconds=fo_result.get("rpo_seconds"),
         docs_lost=fo_result.get("docs_lost"), waited_s=fo_result.get("waited_s"),
         embed_model_version=fo_result.get("embed_model_version"))
    summary["failover"] = {k: fo_result.get(k) for k in
                           ("ok", "steps", "rpo_seconds", "docs_lost", "waited_s",
                            "embed_model_version", "failed_step", "error")}
    if not fo_result.get("ok"):
        step(3, "failover_failed", failed_step=fo_result.get("failed_step"),
             error=fo_result.get("error"),
             note="KHONG retry tu dong — can nguoi xem xet, tranh flapping")
        return summary

    # ---- 4. verify_state_replica: ĐỌC kết quả từ bước 3, không gọi lại ----
    ts_after = fo_result.get("target_state") or {}
    step(4, "verify_state_replica",
         vector_count=ts_after.get("count"), weights=ts_after.get("weights"),
         pool_state=ts_after.get("pool_state"),
         docs_lost=fo_result.get("docs_lost"), rpo_seconds=fo_result.get("rpo_seconds"))
    summary["replica_state"] = ts_after

    # ---- 5. dns_cutover: đọc lại kết quả cutover ----
    active = pathlib.Path("edge/active_region").read_text().strip() \
        if pathlib.Path("edge/active_region").exists() else None
    step(5, "dns_cutover", active_region=active,
         cutover_ok=(active == target), edge_file="edge/active_region")
    summary["active_region"] = active

    # ---- 6. verify_golden_signals: 10 request thật vào region phụ ----
    latencies, errors = [], 0
    with httpx.Client(timeout=5.0) as c:
        for i in range(10):
            t0 = time.time()
            try:
                r = c.get(f"{URL[target]}/v1/infer", params={"q": f"golden signal {i}"})
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.time() - t0) * 1000)
    lat_sorted = sorted(latencies)
    p95 = round(lat_sorted[max(0, int(0.95 * len(lat_sorted)) - 1)], 1)
    step(6, "verify_golden_signals", requests=10, errors=errors,
         error_rate=round(errors / 10, 2), p95_latency_ms=p95,
         p50_latency_ms=round(lat_sorted[len(lat_sorted) // 2], 1))
    summary["golden_signals"] = {"p95_latency_ms": p95, "error_rate": round(errors / 10, 2)}

    # ---- 7. post_incident ----
    elapsed = round(time.time() - incident_ts, 2)
    step(7, "post_incident", elapsed_s=elapsed, active_region=active,
         rto_command=f"python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     f"--target-rto 300",
         note="do RTO/RPO tu log, viet postmortem blameless")
    summary.update(ok=True, elapsed_s=elapsed)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
