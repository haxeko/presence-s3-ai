"""Phương pháp ngưỡng (đối chứng và phương án khi chưa có mô hình), giống ``threshold_detector.cpp``.

Một mẫu "vượt ngưỡng" nếu có gate g với m_g > thr_m[g] hoặc s_g > thr_s[g] (ngưỡng 100 = tắt gate).
Điểm của cửa sổ = số mẫu vượt ngưỡng / số mẫu hợp lệ (float32), rồi qua cùng logic giữ trạng thái.

Hai bộ ngưỡng:
* "factory": ngưỡng xuất xưởng của LD2410 (tĩnh gate 0-1 tắt vì radar không cho chỉnh);
* "baseline": tính từ một phiên "Học phòng trống" do người dùng chủ động chạy:
  thr = min(100, đỉnh + max(BASELINE_MARGIN_MIN, đỉnh - trung bình)), luôn cao hơn mọi mẫu của phiên nền.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .contract import GATES, PresenceParams
from .ld2410 import FACTORY_MOVING, FACTORY_STILL

DISABLED = 100
BASELINE_MARGIN_MIN = 8

METHOD_NONE = 0
METHOD_THRESHOLD_FACTORY = 1
METHOD_THRESHOLD_BASELINE = 2
METHOD_MODEL = 3
METHOD_NAMES = {
    METHOD_NONE: "none",
    METHOD_THRESHOLD_FACTORY: "threshold_factory",
    METHOD_THRESHOLD_BASELINE: "threshold_baseline",
    METHOD_MODEL: "model",
}

# Tham số giữ trạng thái mặc định cho phương pháp ngưỡng (điểm = tỉ lệ mẫu vượt ngưỡng)
THRESHOLD_PRESENCE_DEFAULTS = PresenceParams(enter=0.5, exit=0.2, confirm_occupied_ms=2000, confirm_empty_ms=60000)


@dataclass
class Thresholds:
    moving: list[int] = field(default_factory=lambda: list(FACTORY_MOVING))
    still: list[int] = field(default_factory=lambda: [DISABLED, DISABLED] + list(FACTORY_STILL[2:]))

    def validate(self) -> None:
        if len(self.moving) != GATES or len(self.still) != GATES:
            raise ValueError("cần 9 ngưỡng mỗi loại")
        if any(not (0 <= v <= 100) for v in self.moving + self.still):
            raise ValueError("ngưỡng 0..100")

    def as_dict(self) -> dict:
        return {"moving": list(self.moving), "still": list(self.still)}


def factory_thresholds() -> Thresholds:
    return Thresholds()


def sample_hits(energies: np.ndarray, thr: Thresholds) -> np.ndarray:
    """energies (..., 18) -> bool (...)"""
    e = np.asarray(energies).astype(np.int32)
    tm = np.asarray(thr.moving, dtype=np.int32)
    ts = np.asarray(thr.still, dtype=np.int32)
    hit_m = (e[..., :GATES] > tm) & (tm < DISABLED)
    hit_s = (e[..., GATES:] > ts) & (ts < DISABLED)
    return hit_m.any(axis=-1) | hit_s.any(axis=-1)


def window_score(samples: np.ndarray, valid: np.ndarray, thr: Thresholds) -> np.float32:
    v = np.asarray(valid, dtype=bool)
    n = int(v.sum())
    if n == 0:
        raise ValueError("empty window")
    hits = int((sample_hits(samples, thr) & v).sum())
    return np.float32(hits) / np.float32(n)


def window_scores_batch(samples: np.ndarray, valid: np.ndarray, thr: Thresholds) -> np.ndarray:
    v = np.asarray(valid, dtype=bool)
    n = v.sum(axis=1)
    hits = (sample_hits(samples, thr) & v).sum(axis=1)
    return (hits.astype(np.float32) / np.maximum(n, 1).astype(np.float32)).astype(np.float32)


def baseline_stats(energies: np.ndarray) -> dict:
    """Thống kê nền theo kênh từ các mẫu (N, 18) của một phiên phòng trống."""
    e = np.asarray(energies, dtype=np.float64)
    if e.ndim != 2 or e.shape[1] != 2 * GATES or len(e) == 0:
        raise ValueError("cần mảng (N, 18) không rỗng")
    q = np.percentile(e, [50, 95, 99], axis=0)
    return {
        "n_samples": int(len(e)),
        "mean": np.round(e.mean(axis=0), 3).tolist(),
        "std": np.round(e.std(axis=0), 3).tolist(),
        "p50": np.round(q[0], 3).tolist(),
        "p95": np.round(q[1], 3).tolist(),
        "p99": np.round(q[2], 3).tolist(),
        "max": e.max(axis=0).astype(int).tolist(),
    }


def thresholds_from_baseline(stats: dict, margin_min: int = BASELINE_MARGIN_MIN) -> Thresholds:
    peak = np.asarray(stats["max"], dtype=np.float64)
    mean = np.asarray(stats["mean"], dtype=np.float64)
    margin = np.maximum(margin_min, peak - mean)
    thr = np.minimum(DISABLED, np.ceil(peak + margin)).astype(int).tolist()
    return Thresholds(moving=thr[:GATES], still=thr[GATES:])
