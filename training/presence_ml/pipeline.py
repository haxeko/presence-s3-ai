"""Phát lại chuỗi thời gian qua toàn bộ chuỗi quyết định, giống ``firmware/main/core/pipeline.cpp``.

Mỗi đoạn (một boot) được chạy lại từ đầu như thiết bị vừa khởi động: cửa sổ 20 ô trượt theo tick, cứ hết
một hop (tick % 5 == 4) thì tính điểm bằng từng phương pháp rồi qua logic giữ trạng thái riêng của phương pháp đó.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from . import contract as C
from .dataio import Segment
from .presence_logic import PresenceLogic
from .threshold import Thresholds, window_scores_batch

# scorer(samples (N,20,18), valid (N,20), features (N,72)) -> (score (N,) float32, extra dict of arrays)
Scorer = Callable[[np.ndarray, np.ndarray, np.ndarray], tuple[np.ndarray, dict]]


@dataclass
class Method:
    name: str
    scorer: Scorer
    params: C.PresenceParams


def threshold_scorer(thr: Thresholds) -> Scorer:
    def score(samples, valid, feats):
        return window_scores_batch(samples, valid, thr), {}

    return score


def int8_scorer(contract: C.ContractBlock, run_int8: Callable[[np.ndarray], np.ndarray]) -> Scorer:
    def score(samples, valid, feats):
        z = C.normalize(feats, contract.feature_mean, contract.feature_std)
        q_in = C.quantize_input(z, contract.input_scale, contract.input_zero_point)
        q_out = np.asarray(run_int8(q_in), dtype=np.int8)
        s = C.occupied_score_from_q(q_out, contract.output_scale, contract.output_zero_point)
        return s, {"q_out": q_out, "pred": np.argmax(q_out, axis=1)}

    return score


def float_scorer(mean: np.ndarray, std: np.ndarray, predict: Callable[[np.ndarray], np.ndarray]) -> Scorer:
    def score(samples, valid, feats):
        p = np.asarray(predict(C.normalize(feats, mean, std)), dtype=np.float32)
        s = np.zeros(len(p), dtype=np.float32)
        for c in C.OCCUPIED_CLASSES:
            s = s + p[:, c]
        return s, {"probs": p, "pred": np.argmax(p, axis=1)}

    return score


def decision_windows(seg: Segment, hop: int = C.HOP_SLOTS):
    """Các điểm quyết định của một đoạn: (chỉ số ô cuối, samples (N,20,18), valid (N,20))."""
    W = C.WINDOW_SLOTS
    T = seg.n_slots
    ends = np.array([e for e in range(T) if (seg.tick0 + e + 1) % hop == 0], dtype=np.int64)
    if len(ends) == 0:
        return ends, np.zeros((0, W, C.N_CHANNELS), np.uint8), np.zeros((0, W), bool)
    pad_e = np.concatenate([np.zeros((W - 1, C.N_CHANNELS), np.uint8), seg.energy])
    pad_v = np.concatenate([np.zeros(W - 1, bool), seg.valid])
    gather = ends[:, None] + np.arange(W)[None, :]  # chỉ số trong mảng đã đệm
    return ends, pad_e[gather], pad_v[gather]


def replay_segment(seg: Segment, methods: list[Method], hop: int = C.HOP_SLOTS,
                   min_valid: int = C.MIN_VALID_SLOTS, max_gap: int = C.MAX_GAP_SLOTS) -> pd.DataFrame:
    ends, samples, valid = decision_windows(seg, hop)
    n = len(ends)
    usable = np.array([C.window_is_usable(v, min_valid, max_gap) for v in valid], dtype=bool)
    feats = np.zeros((n, C.N_FEATURES), dtype=np.float32)
    if usable.any():
        feats[usable] = C.compute_features_batch(samples[usable], valid[usable])
    out = pd.DataFrame({
        "session_id": seg.session_id,
        "boot_id": seg.boot_id,
        "tick": seg.tick0 + ends,
        "ts_ms": seg.ts_ms[ends] if n else np.zeros(0, np.int64),
        "truth": seg.label[ends] if n else np.zeros(0, np.int64),
        "usable": usable,
    })
    hop_ms = hop * C.SAMPLE_PERIOD_MS
    for m in methods:
        score = np.full(n, np.nan, dtype=np.float32)
        pred = np.full(n, -1, dtype=np.int64)
        if usable.any():
            s, extra = m.scorer(samples[usable], valid[usable], feats[usable])
            score[usable] = s
            if "pred" in extra:
                pred[usable] = extra["pred"]
            if "q_out" in extra:
                q = np.zeros((n, C.N_CLASSES), dtype=np.int64)
                q[usable] = extra["q_out"]
                for k in range(C.N_CLASSES):
                    out[f"{m.name}_q{k}"] = q[:, k]
        logic = PresenceLogic(C.PresenceParams(**vars(m.params)), hop_ms)
        states = np.zeros(n, dtype=np.int64)
        decisions = np.zeros(n, dtype=np.int64)
        for i in range(n):
            r = logic.step(float(score[i]) if usable[i] else None)
            states[i] = r.state
            decisions[i] = r.decision
        out[f"{m.name}_score"] = score
        out[f"{m.name}_pred"] = pred
        out[f"{m.name}_state"] = states
        out[f"{m.name}_decision"] = decisions
    return out


def replay(segments: list[Segment], methods: list[Method], **kw) -> pd.DataFrame:
    parts = [replay_segment(s, methods, **kw) for s in segments]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
