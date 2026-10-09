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
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    """Đọc /v1/state của region: pool_state? weights? vector count?"""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
        return r.json()
    except Exception as e:  # process chết / netblock: vẫn trả dict để bước 1 không crash
        return {"region": region, "error": type(e).__name__}



def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước failover theo ĐÚNG thứ tự: verify -> restore -> scale -> wait -> cutover.

    Trả về dict có: ok, các bước đã xong, rpo_seconds/docs_lost (bước 2),
    waited_s (bước 4) và target_state cuối (cho runbook verify).
    Bước 4 timeout -> ok=False và KHÔNG BAO GIỜ đụng vào edge/active_region.
    """
    primary = "a" if target == "b" else "b"
    result = {"ok": False, "target": target, "primary": primary, "steps": []}

    # ---- Bước 1: verify_target — region phụ đang ở trạng thái nào? ----
    before = state_of(target)
    emit(step="1_verify_target", target=target, target_state_before=before)
    result["steps"].append("1_verify_target")
    result["target_state_before"] = before

    # ---- Bước 2: restore_snapshot — copy dữ liệu backup vào region phụ ----
    try:
        meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    except SystemExit as e:  # chưa từng có snapshot `put` nào
        emit(step="2_restore_snapshot", failed=True, error=str(e))
        result.update(failed_step="2_restore_snapshot", error=str(e))
        return result
    emit(step="2_restore_snapshot", snapshot_at=meta.get("snapshot_at"),
         restored_at=meta.get("restored_at"),
         embed_model_version=meta.get("embed_model_version"),
         rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"))
    result["steps"].append("2_restore_snapshot")
    result.update(rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
                  embed_model_version=meta.get("embed_model_version"))

    # ---- Bước 3: scale_pool — warm -> full để bật GPU pool ----
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.write_text("full")
    emit(step="3_scale_pool", pool_state="full", pool_state_file=str(pool_file))
    result["steps"].append("3_scale_pool")

    # ---- Bước 4: wait_ready — QUAN TRỌNG NHẤT: chờ /readyz trả 200 thật ----
    start = time.time()
    deadline = start + wait
    ready, last_reason = False, None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{URL[target]}/readyz",
                          timeout=min(2.0, max(0.5, deadline - time.time())))
            if r.status_code == 200:
                ready = True
                break
            try:
                last_reason = r.json().get("reasons")
            except Exception:
                last_reason = f"http_{r.status_code}"
        except Exception as e:
            last_reason = type(e).__name__
        time.sleep(0.5)
    waited = round(time.time() - start, 2)
    emit(step="4_wait_ready", ready=ready, waited_s=waited,
         reason=None if ready else last_reason)
    result["steps"].append("4_wait_ready")
    result["waited_s"] = waited
    if not ready:
        # BẪY: target chưa ready mà cutover thì user ăn 503 từ CẢ HAI phía -> ABORT.
        emit(step="ABORT", why=f"4_wait_ready timeout sau {waited}s -> KHONG cutover")
        result.update(failed_step="4_wait_ready", ready=False,
                      error=f"target {target} khong ready sau {waited}s")
        return result

    # ---- Bước 5: dns_cutover — chỉ bây giờ mới ghi edge/active_region ----
    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", active_region=target, file="edge/active_region")
    result["steps"].append("5_dns_cutover")

    result.update(ok=True, ready=True, cutover=target, target_state=state_of(target))
    return result

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
