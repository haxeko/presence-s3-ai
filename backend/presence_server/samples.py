"""Nhận lô mẫu MQTT: kiểm tra, ước lượng thời gian, chống trùng, ghi CSV của phiên đang thu, giữ bộ đệm trực tiếp.

Hàng (schema v1, 40 số nguyên, docs/data-format.md):
seq, tick, nf, rs, mv_cm, mv_e, st_cm, st_e, det_cm, m0..m8, s0..s8, light, out, ev, meth, mver, pred, q0..q3,
occ, ps, pd.
Thời gian mỗi mẫu: thời điểm cuối ô = t0 + 200 (tick + 1) theo đồng hồ thiết bị; quy ra ms epoch bằng đồng hồ
thiết bị (epoch > 0, đã đồng bộ SNTP), hoặc bằng độ lệch uptime -> giờ backend ước lượng bởi DeviceClock ("server").
"""

from __future__ import annotations

import csv
import io
import threading
from collections import deque
from pathlib import Path

from presence_ml.dataio import SAMPLE_COLUMNS

ROW_SCHEMA = 1
ROW_COLUMNS = 40
ROW_NAMES = (
    ["seq", "tick", "n_frames", "radar_state", "mv_cm", "mv_e", "st_cm", "st_e", "det_cm"]
    + [f"m{g}" for g in range(9)] + [f"s{g}" for g in range(9)]
    + ["light", "out_pin", "ev", "method", "model_version", "pred", "q0", "q1", "q2", "q3", "occ_pm", "p_state",
       "p_decision"]
)
assert len(ROW_NAMES) == ROW_COLUMNS
SAMPLE_PERIOD_MS = 200


class BatchError(ValueError):
    pass


class DeviceClock:
    """Độ lệch giữa uptime của một lần khởi động thiết bị và giờ backend, khi thiết bị CHƯA có SNTP (epoch = 0).

    Mỗi lô cho d = giờ nhận - uptime lúc gửi = độ lệch thật + độ trễ (mạng, hàng đợi của broker), nên d >= độ lệch
    thật. Ước lượng là d nhỏ nhất đã gặp (lô ít trễ nhất), và chỉ được tăng tối đa MAX_DRIFT theo thời gian thiết bị
    để theo kịp trôi của thạch anh. Nhờ vậy, các lô đến muộn hàng phút (backend khởi động lại, broker giữ hàng đợi)
    vẫn được đặt đúng giờ, thay vì lệch theo lúc nhận.
    """

    MAX_DRIFT = 1e-4  # 100 ppm: thạch anh ESP32 lệch cỡ +-10..50 ppm

    def __init__(self, boot_id: int | None = None, offset_ms: float = 0.0, dev_ms: int = 0):
        self.boot_id = boot_id
        self.offset_ms = offset_ms
        self.dev_ms = dev_ms

    def update(self, boot_id: int, rx_ms: int, now_dev_ms: int) -> float:
        d = float(rx_ms - now_dev_ms)
        if boot_id != self.boot_id:
            self.boot_id, self.offset_ms, self.dev_ms = boot_id, d, now_dev_ms
            return self.offset_ms
        ceiling = self.offset_ms + max(0, now_dev_ms - self.dev_ms) * self.MAX_DRIFT
        self.offset_ms = min(d, ceiling)
        self.dev_ms = max(self.dev_ms, now_dev_ms)
        return self.offset_ms


def apply_clock(rows: list[dict], offset_ms: float) -> None:
    """Đặt ts_ms của các mẫu (chưa có SNTP) theo độ lệch ước lượng."""
    for d in rows:
        d["ts_ms"] = int(round(offset_ms + d["t_dev_ms"]))


def parse_batch(msg: dict, rx_ms: int) -> tuple[dict, list[dict]]:
    """Trả về (header, các mẫu dạng dict có ts_ms/ts_src). Ném BatchError khi sai định dạng."""
    if msg.get("v") != ROW_SCHEMA:
        raise BatchError(f"schema {msg.get('v')} không hỗ trợ")
    try:
        dev = str(msg["dev"])
        boot = int(msg["boot"])
        t0 = int(msg["t0"])
        now = int(msg["now"])
        epoch = int(msg.get("epoch") or 0)
        rows = msg["rows"]
    except (KeyError, TypeError, ValueError) as e:
        raise BatchError(f"header: {e}") from e
    if not isinstance(rows, list):
        raise BatchError("rows")
    base = epoch if epoch > 0 else rx_ms
    src = "device" if epoch > 0 else "server"
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) != ROW_COLUMNS or not all(isinstance(x, int) for x in r):
            raise BatchError("row length/type")
        d = dict(zip(ROW_NAMES, r))
        t_dev = t0 + SAMPLE_PERIOD_MS * (d["tick"] + 1)
        d["t_dev_ms"] = t_dev
        d["ts_ms"] = base - (now - t_dev)
        d["ts_src"] = src
        d["device_id"] = dev
        d["boot_id"] = boot
        if any(not (0 <= d[f"{p}{g}"] <= 100) for p in "ms" for g in range(9)):
            raise BatchError("energy out of range")
        out.append(d)
    return {"device_id": dev, "boot_id": boot, "t0": t0, "now": now, "epoch": epoch}, out


class SessionWriter:
    """CSV append-only của một phiên (một file, header lần đầu)."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(SAMPLE_COLUMNS)
        self.count = 0

    def write(self, session_id: str, rows: list[dict]) -> None:
        for d in rows:
            self.w.writerow([session_id if c == "session_id" else d.get(c, "") for c in SAMPLE_COLUMNS])
        self.count += len(rows)
        self.f.flush()

    def close(self) -> None:
        self.f.close()


class LiveBuffer:
    """Mẫu gần đây của từng thiết bị (bộ nhớ), cho biểu đồ trực tiếp và trích đoạn quanh báo giả/bỏ sót."""

    def __init__(self, minutes: int = 15):
        self.maxlen = max(1, minutes) * 60 * 1000 // SAMPLE_PERIOD_MS
        self.lock = threading.Lock()
        self.buf: dict[str, deque] = {}
        self.counter: dict[str, int] = {}
        self.seen: dict[str, deque] = {}

    def add(self, device_id: str, rows: list[dict]) -> list[dict]:
        """Thêm mẫu, bỏ mẫu trùng (cùng boot + seq, do QoS 1 gửi lại). Trả về các mẫu mới."""
        fresh = []
        with self.lock:
            b = self.buf.setdefault(device_id, deque(maxlen=self.maxlen))
            seen = self.seen.setdefault(device_id, deque(maxlen=4096))
            seen_set = set(seen)
            for d in rows:
                key = (d["boot_id"], d["seq"])
                if key in seen_set:
                    continue
                seen.append(key)
                seen_set.add(key)
                self.counter[device_id] = self.counter.get(device_id, 0) + 1
                d["_n"] = self.counter[device_id]
                b.append(d)
                fresh.append(d)
        return fresh

    def since(self, device_id: str, cursor: int, limit: int = 2000) -> list[dict]:
        with self.lock:
            b = self.buf.get(device_id)
            if not b:
                return []
            return [d for d in b if d["_n"] > cursor][-limit:]

    def window(self, device_id: str, start_ms: int, end_ms: int) -> list[dict]:
        with self.lock:
            return [d for d in self.buf.get(device_id, ()) if start_ms <= d["ts_ms"] < end_ms]


def rows_to_csv(session_id: str, rows: list[dict]) -> str:
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(SAMPLE_COLUMNS)
    for d in rows:
        w.writerow([session_id if c == "session_id" else d.get(c, "") for c in SAMPLE_COLUMNS])
    return s.getvalue()
