"""Chia train/validation/test theo PHIÊN hoặc theo NGÀY, trước khi tạo cửa sổ.

Mọi cửa sổ của một nhóm (phiên hoặc ngày) nằm trọn trong một tập, nên các cửa sổ chồng lấn của cùng
phiên không bao giờ lọt sang hai tập. Phiên có ``split`` đặt sẵn (trên dashboard) được giữ nguyên.
Các nhóm còn lại được chia ngẫu nhiên có hạt giống: thử nhiều hoán vị, chọn cách chia ít thiếu lớp nhất
trong mỗi tập và gần tỉ lệ mục tiêu nhất (tính theo thời lượng có nhãn).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import contract as C
from .dataio import SessionMeta

SPLITS = ("train", "val", "test")


@dataclass
class SplitResult:
    assignment: dict[str, str]  # session_id -> split
    groups: dict[str, str]  # group -> split
    group_of: dict[str, str]  # session_id -> group
    class_seconds: dict[str, dict[str, float]]  # split -> class -> seconds
    warnings: list[str]


def group_key(meta: SessionMeta, mode: str) -> str:
    if mode == "session":
        return meta.session_id
    if mode == "day":
        return f"{meta.device_id}:{meta.day}"
    raise ValueError("split mode must be 'session' or 'day'")


def split_sessions(
    sessions: list[SessionMeta],
    class_seconds: dict[str, dict[str, float]],
    mode: str = "session",
    ratios: tuple[float, float, float] = (0.7, 0.15, 0.15),
    seed: int = 42,
    trials: int = 400,
) -> SplitResult:
    """class_seconds: session_id -> {class: giây có nhãn}."""
    group_of = {s.session_id: group_key(s, mode) for s in sessions}
    gsec: dict[str, np.ndarray] = {}
    fixed: dict[str, str] = {}
    warnings: list[str] = []
    for s in sessions:
        g = group_of[s.session_id]
        v = np.array([class_seconds.get(s.session_id, {}).get(c, 0.0) for c in C.CLASSES])
        gsec[g] = gsec.get(g, np.zeros(len(C.CLASSES))) + v
        if s.split in SPLITS:
            if g in fixed and fixed[g] != s.split:
                warnings.append(f"nhóm {g} có phiên đặt sẵn ở cả {fixed[g]} và {s.split}: dùng {fixed[g]}")
            else:
                fixed[g] = s.split
    groups = sorted(gsec)
    free = [g for g in groups if g not in fixed]
    total = sum(gsec[g].sum() for g in groups) or 1.0
    rng = np.random.default_rng(seed)
    target = np.asarray(ratios, dtype=float) / sum(ratios)
    best = None
    for t in range(max(1, trials)):
        order = list(free)
        if t:
            rng.shuffle(order)
        assign = dict(fixed)
        acc = np.zeros(3)
        for g in fixed:
            acc[SPLITS.index(fixed[g])] += gsec[g].sum()
        for g in order:
            # vào tập đang thiếu nhiều nhất so với mục tiêu
            deficit = target * total - acc
            k = int(np.argmax(deficit))
            assign[g] = SPLITS[k]
            acc[k] += gsec[g].sum()
        per = np.zeros((3, len(C.CLASSES)))
        for g, sp in assign.items():
            per[SPLITS.index(sp)] += gsec[g]
        present = per.sum(axis=0) > 0  # lớp có trong dữ liệu
        missing = int(((per == 0) & present[None, :]).sum())
        dev = float(np.abs(acc / total - target).sum())
        score = (missing, dev)
        if best is None or score < best[0]:
            best = (score, assign, per)
    _, assign, per = best
    class_sec = {sp: {c: float(per[i, j]) for j, c in enumerate(C.CLASSES)} for i, sp in enumerate(SPLITS)}
    for sp in SPLITS:
        for c in C.CLASSES:
            if class_sec[sp][c] == 0 and any(class_sec[o][c] > 0 for o in SPLITS):
                warnings.append(f"tập {sp} không có lớp {c}")
    # Kiểm tra cuối: không nhóm nào thuộc hai tập
    assignment = {s.session_id: assign[group_of[s.session_id]] for s in sessions}
    for g in groups:
        sps = {assignment[s] for s, gg in group_of.items() if gg == g}
        assert len(sps) == 1, f"group {g} in several splits"
    return SplitResult(assignment=assignment, groups=assign, group_of=group_of, class_seconds=class_sec,
                       warnings=warnings)
