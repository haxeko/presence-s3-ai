"""Tạo cửa sổ 4 s (20 ô), bước 1 s (5 ô) từ các đoạn; loại cửa sổ không dùng được và đếm lý do.

Cửa sổ kết thúc ở tick e với (e + 1) % HOP == 0 (giống thiết bị), gồm ô [e - 19, e].
Bị loại (không dùng để huấn luyện/đánh giá theo cửa sổ) khi:
* thiếu dữ liệu: < MIN_VALID_SLOTS ô có mẫu hoặc có khoảng thiếu > MAX_GAP_SLOTS ô ("missing");
* có ô chưa gán nhãn ("unlabeled") hoặc nhãn xung đột ("conflict");
* chứa hai nhãn khác nhau ("mixed");
* có ô cách thời điểm chuyển nhãn < transition_guard_slots ("transition");
Không nhân bản cửa sổ hay mẫu để cân bằng lớp (dùng class weight khi huấn luyện).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from . import contract as C
from .dataio import LABEL_CONFLICT, LABEL_UNLABELED, Segment


@dataclass
class WindowSet:
    samples: np.ndarray  # (N, 20, 18) uint8
    valid: np.ndarray  # (N, 20) bool
    label: np.ndarray  # (N,) int
    session: np.ndarray  # (N,) object
    end_tick: np.ndarray  # (N,) int
    end_ts_ms: np.ndarray  # (N,) int
    rejected: Counter

    def __len__(self) -> int:
        return len(self.label)

    @staticmethod
    def empty() -> "WindowSet":
        return WindowSet(np.zeros((0, C.WINDOW_SLOTS, C.N_CHANNELS), np.uint8), np.zeros((0, C.WINDOW_SLOTS), bool),
                         np.zeros(0, np.int64), np.zeros(0, object), np.zeros(0, np.int64), np.zeros(0, np.int64),
                         Counter())

    @staticmethod
    def concat(parts: list["WindowSet"]) -> "WindowSet":
        parts = [p for p in parts if p is not None]
        if not parts:
            return WindowSet.empty()
        rej = Counter()
        for p in parts:
            rej.update(p.rejected)
        return WindowSet(
            np.concatenate([p.samples for p in parts]),
            np.concatenate([p.valid for p in parts]),
            np.concatenate([p.label for p in parts]),
            np.concatenate([p.session for p in parts]),
            np.concatenate([p.end_tick for p in parts]),
            np.concatenate([p.end_ts_ms for p in parts]),
            rej,
        )


def transition_mask(label: np.ndarray, guard: int) -> np.ndarray:
    """True cho ô cách một điểm đổi nhãn (giữa hai lớp hợp lệ, hoặc lớp <-> chưa gán nhãn) dưới `guard` ô."""
    near = np.zeros(len(label), dtype=bool)
    if guard <= 0 or len(label) < 2:
        return near
    change = np.nonzero(label[1:] != label[:-1])[0] + 1
    for b in change:
        near[max(0, b - guard): min(len(label), b + guard)] = True
    return near


def windows_from_segment(seg: Segment, transition_guard_slots: int = 10, min_valid: int = C.MIN_VALID_SLOTS,
                         max_gap: int = C.MAX_GAP_SLOTS, hop: int = C.HOP_SLOTS,
                         require_label: bool = True) -> WindowSet:
    W = C.WINDOW_SLOTS
    T = seg.n_slots
    rej: Counter = Counter()
    if T < W:
        rej["short_segment"] += 1
        ws = WindowSet.empty()
        ws.rejected = rej
        return ws
    near = transition_mask(seg.label, transition_guard_slots)
    ends = [e for e in range(W - 1, T) if (seg.tick0 + e + 1) % hop == 0]
    keep = []
    labels = []
    for e in ends:
        s = e - W + 1
        v = seg.valid[s: e + 1]
        if not C.window_is_usable(v, min_valid, max_gap):
            rej["missing"] += 1
            continue
        lab = seg.label[s: e + 1]
        if require_label:
            if np.any(lab == LABEL_CONFLICT):
                rej["conflict"] += 1
                continue
            if np.any(lab == LABEL_UNLABELED):
                rej["unlabeled"] += 1
                continue
            if np.any(lab != lab[0]):
                rej["mixed"] += 1
                continue
            if np.any(near[s: e + 1]):
                rej["transition"] += 1
                continue
        keep.append(e)
        labels.append(int(lab[-1]))
    if not keep:
        ws = WindowSet.empty()
        ws.rejected = rej
        return ws
    idx = np.asarray(keep)
    starts = idx - W + 1
    gather = starts[:, None] + np.arange(W)[None, :]
    return WindowSet(
        samples=seg.energy[gather],
        valid=seg.valid[gather],
        label=np.asarray(labels, dtype=np.int64),
        session=np.full(len(idx), seg.session_id, dtype=object),
        end_tick=seg.tick0 + idx,
        end_ts_ms=seg.ts_ms[idx],
        rejected=rej,
    )
