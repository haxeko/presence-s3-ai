"""Chỉ số đánh giá: theo cửa sổ và theo dòng thời gian (sau logic giữ trạng thái).

Theo cửa sổ: confusion matrix 4 lớp, precision/recall/F1 từng lớp, kết quả nhị phân sau khi gộp lớp.
Theo dòng thời gian (mỗi bước 1 s, chỉ tính thời gian có nhãn người xác nhận):
* báo giả: mỗi đoạn liên tục mà quyết định = có người trong khi nhãn = phòng trống; tính số lần/giờ phòng trống;
* bỏ sót: thời gian nhãn = có người mà quyết định = trống; khoảng bỏ sót dài nhất;
* độ trễ vào phòng: từ lúc nhãn đổi trống -> có người tới khi quyết định = có người (giới hạn trên cap);
* độ trễ ra khỏi phòng: từ lúc nhãn đổi có người -> trống tới khi quyết định = trống;
* thời gian "chưa chắc" và thời gian chưa có quyết định.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import contract as C
from .presence_logic import DECISION_EMPTY, DECISION_OCCUPIED, DECISION_UNKNOWN, STATE_UNCERTAIN


def confusion(y_true: np.ndarray, y_pred: np.ndarray, n: int) -> np.ndarray:
    m = np.zeros((n, n), dtype=np.int64)
    for t, p in zip(np.asarray(y_true, int), np.asarray(y_pred, int)):
        m[t, p] += 1
    return m


def per_class(m: np.ndarray, names: list[str]) -> dict:
    out = {}
    for k, name in enumerate(names):
        tp = int(m[k, k])
        fp = int(m[:, k].sum() - tp)
        fn = int(m[k, :].sum() - tp)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
        out[name] = {"precision": _r(prec), "recall": _r(rec), "f1": _r(f1), "support": int(m[k, :].sum())}
    return out


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def window_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    y_true = np.asarray(y_true, int)
    y_pred = np.asarray(y_pred, int)
    if len(y_true) == 0:
        return {"windows": 0}
    m4 = confusion(y_true, y_pred, C.N_CLASSES)
    occ_t = np.isin(y_true, C.OCCUPIED_CLASSES).astype(int)
    occ_p = np.isin(y_pred, C.OCCUPIED_CLASSES).astype(int)
    m2 = confusion(occ_t, occ_p, 2)
    return {
        "windows": int(len(y_true)),
        "accuracy": _r((y_true == y_pred).mean()),
        "confusion": m4.tolist(),
        "per_class": per_class(m4, C.CLASSES),
        "binary": {
            "confusion": m2.tolist(),  # hàng: thật (0 trống, 1 có người), cột: dự đoán
            "accuracy": _r((occ_t == occ_p).mean()),
            "per_class": per_class(m2, ["empty", "occupied"]),
        },
    }


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Các đoạn True liên tục: (start, end) end không gồm."""
    runs = []
    i = 0
    n = len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def timeline_metrics(df: pd.DataFrame, method: str, hop_s: float = 1.0, entry_cap_s: float = 60,
                     exit_cap_s: float = 600) -> dict:
    """df: kết quả pipeline.replay; chỉ các bước có nhãn (truth >= 0) được tính."""
    tot = {"labeled_s": 0.0, "empty_s": 0.0, "occupied_s": 0.0, "false_alarms": 0, "false_alarm_s": 0.0,
           "missed_s": 0.0, "longest_miss_s": 0.0, "uncertain_s": 0.0, "undecided_s": 0.0}
    entries: list[float | None] = []
    exits: list[float | None] = []
    for (_, _), g in df.groupby(["session_id", "boot_id"], sort=False):
        truth = g["truth"].to_numpy()
        dec = g[f"{method}_decision"].to_numpy()
        state = g[f"{method}_state"].to_numpy()
        labeled = truth >= 0
        occ_t = np.isin(truth, C.OCCUPIED_CLASSES)
        emp_t = labeled & ~occ_t
        tot["labeled_s"] += labeled.sum() * hop_s
        tot["empty_s"] += emp_t.sum() * hop_s
        tot["occupied_s"] += occ_t.sum() * hop_s
        tot["uncertain_s"] += (labeled & (state == STATE_UNCERTAIN)).sum() * hop_s
        tot["undecided_s"] += (labeled & (dec == DECISION_UNKNOWN)).sum() * hop_s
        fa = emp_t & (dec == DECISION_OCCUPIED)
        fa_runs = _runs(fa)
        tot["false_alarms"] += len(fa_runs)
        tot["false_alarm_s"] += fa.sum() * hop_s
        miss = occ_t & (dec == DECISION_EMPTY)
        tot["missed_s"] += miss.sum() * hop_s
        for a, b in _runs(miss):
            tot["longest_miss_s"] = max(tot["longest_miss_s"], (b - a) * hop_s)
        # chuyển trạng thái theo nhãn (giữa hai bước có nhãn liền nhau)
        for i in range(1, len(truth)):
            if not (labeled[i] and labeled[i - 1]):
                continue
            if occ_t[i] and not occ_t[i - 1]:
                entries.append(_latency(dec, i, DECISION_OCCUPIED, occ_t, hop_s, entry_cap_s))
            elif not occ_t[i] and occ_t[i - 1]:
                exits.append(_latency(dec, i, DECISION_EMPTY, emp_t, hop_s, exit_cap_s))
    empty_h = tot["empty_s"] / 3600
    tot["false_alarms_per_hour"] = _r(tot["false_alarms"] / empty_h, 3) if empty_h > 0 else None
    tot["entry_latency_s"] = _latency_stats(entries)
    tot["exit_latency_s"] = _latency_stats(exits)
    return {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}


def _latency(dec, i, target, still_true, hop_s, cap_s):
    """Thời gian từ bước i tới khi dec == target, trong lúc nhãn vẫn đúng; None nếu không đạt trước cap."""
    j = i
    while j < len(dec) and still_true[j] and (j - i) * hop_s <= cap_s:
        if dec[j] == target:
            return (j - i) * hop_s
        j += 1
    return None


def _latency_stats(values: list[float | None]) -> dict:
    ok = [v for v in values if v is not None]
    return {
        "transitions": len(values),
        "detected": len(ok),
        "not_detected_within_cap": len(values) - len(ok),
        "median": _r(float(np.median(ok)), 2) if ok else None,
        "p90": _r(float(np.percentile(ok, 90)), 2) if ok else None,
        "max": _r(float(np.max(ok)), 2) if ok else None,
    }


def quantization_comparison(float_probs: np.ndarray, q_out: np.ndarray, out_scale: float, out_zp: int,
                            y_true: np.ndarray) -> dict:
    deq = C.dequantize_output(q_out, out_scale, out_zp)
    pf = np.argmax(float_probs, axis=1)
    pq = np.argmax(q_out, axis=1)
    sf = float_probs[:, list(C.OCCUPIED_CLASSES)].sum(axis=1)
    sq = C.occupied_score_from_q(q_out, out_scale, out_zp)
    return {
        "windows": int(len(pf)),
        "argmax_agreement": _r((pf == pq).mean()) if len(pf) else None,
        "max_abs_prob_diff": _r(np.abs(deq - float_probs).max()) if len(pf) else None,
        "max_abs_occupied_score_diff": _r(np.abs(sf - sq).max()) if len(pf) else None,
        "accuracy_float": _r((pf == y_true).mean()) if len(pf) else None,
        "accuracy_int8": _r((pq == y_true).mean()) if len(pf) else None,
    }


def calibration_table(scores: np.ndarray, occupied: np.ndarray, max_points: int = C.CALIB_MAX_POINTS) -> dict:
    """Hiệu chuẩn điểm có người -> xác suất bằng hồi quy isotonic trên tập validation.

    Trả về {} khi dữ liệu không đủ (cần >= 50 cửa sổ, có cả hai lớp).
    """
    scores = np.asarray(scores, dtype=np.float64)
    occupied = np.asarray(occupied, dtype=np.float64)
    if len(scores) < 50 or occupied.min() == occupied.max():
        return {}
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(scores, occupied)
    xs = np.unique(np.quantile(scores, np.linspace(0, 1, max_points)))
    if len(xs) < 2:
        return {}
    ys = iso.predict(xs)
    return {"x": [round(float(x), 5) for x in xs], "y": [round(float(y), 5) for y in ys], "n": int(len(scores)),
            "method": "isotonic (validation)"}


def ece(scores: np.ndarray, occupied: np.ndarray, bins: int = 10) -> float | None:
    """Expected calibration error của điểm có người (coi điểm như xác suất)."""
    s = np.asarray(scores, dtype=np.float64)
    y = np.asarray(occupied, dtype=np.float64)
    if len(s) == 0:
        return None
    idx = np.minimum((s * bins).astype(int), bins - 1)
    e = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            e += m.mean() * abs(s[m].mean() - y[m].mean())
    return _r(e)
