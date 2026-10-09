"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """TODO: trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    """
    Dùng /readyz (KHÔNG dùng /healthz): process sống chưa chắc serve được —
    region thiếu weights / vector DB rỗng / pool chưa full vẫn là NOT READY.
    """
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.ConnectError:
        return False, "connect_error"
    except Exception as e:  # mọi lỗi mạng khác: coi như chưa ready
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons", [])
    except Exception:
        reasons = []
    return False, f"http_{r.status_code}:" + ",".join(reasons)


def emit(out: pathlib.Path, **kw):
    """Ghi 1 dòng JSONL — chỉ gọi khi TRẠNG THÁI ĐỔI, không gọi mỗi lần poll."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "event": "state_change", **kw}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("HEALTH", json.dumps(rec), flush=True)
    return rec


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    - Mỗi `interval` giây: probe CẢ HAI region.
    - Chỉ đổi sang UNHEALTHY sau `threshold` lần fail LIÊN TIẾP (chống flapping).
    - Một lần probe OK khi đang UNHEALTHY -> phục hồi HEALTHY ngay.
    - Chỉ ghi log khi trạng thái THẬT SỰ đổi (kèm interval_s + threshold để
      tools/measure_rto.py tính được detect floor).
    """
    # Giả định ban đầu: các region HEALTHY cho tới khi chứng minh ngược lại.
    state = {r: {"status": "HEALTHY", "fails": 0} for r in URL}
    end = time.time() + duration
    while time.time() < end:
        loop_start = time.time()
        for region in URL:
            ready, reason = probe(region, timeout)  # gọi qua tên module-level để test monkeypatch được
            s = state[region]
            if ready:
                s["fails"] = 0
                if s["status"] != "HEALTHY":
                    s["status"] = "HEALTHY"
                    emit(out, region=region, to="HEALTHY", from_state="UNHEALTHY",
                         reason=reason, interval_s=interval, threshold=threshold,
                         consecutive_fails=0)
            else:
                s["fails"] += 1
                if s["fails"] >= threshold and s["status"] != "UNHEALTHY":
                    s["status"] = "UNHEALTHY"
                    emit(out, region=region, to="UNHEALTHY", from_state="HEALTHY",
                         reason=f"{threshold} probe /readyz fail lien tiep ({reason})",
                         interval_s=interval, threshold=threshold,
                         consecutive_fails=s["fails"])
        # Giữ đúng nhịp interval (probe có thể tốn thời gian, vd timeout 2s khi netblock)
        remaining = interval - (time.time() - loop_start)
        if remaining > 0:
            time.sleep(min(remaining, max(0.0, end - time.time())))

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
