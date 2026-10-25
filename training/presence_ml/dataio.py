"""Đọc bộ dữ liệu (dataset.json + samples.csv của từng phiên), kiểm tra chất lượng và dựng chuỗi ô thời gian.

Định dạng (docs/data-format.md):
* ``dataset.json``: danh sách phiên, nhãn theo khoảng thời gian (ms epoch), phiên nền, ghi chú.
  Backend tạo file này cho mỗi tác vụ huấn luyện; ``presence_ml.sim`` tạo bộ dữ liệu mô phỏng cùng định dạng.
* ``samples.csv``: một dòng mỗi mẫu 5 Hz (cột trong SAMPLE_COLUMNS).

Quy ước dữ liệu không đều / thiếu:
* Ô thời gian xác định bởi ``tick`` (200 ms) của thiết bị trong một lần khởi động (``boot_id``); không nội suy,
  không lặp mẫu cũ. Tick thiếu => ô thiếu. Tick trùng => giữ mẫu đầu, đếm lỗi.
* Khác boot_id => đoạn riêng, không có cửa sổ nào nối qua hai đoạn.
* Nhãn của một ô lấy theo thời điểm (ms) của ô; ô ngoài mọi khoảng nhãn => chưa gán nhãn (-1),
  ô nằm trong hai nhãn khác nhau => xung đột (-2). Dữ liệu chưa gán nhãn KHÔNG được coi là phòng trống.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import contract as C

ENERGY_COLUMNS = list(C.CHANNELS)
SAMPLE_COLUMNS = (
    ["session_id", "device_id", "boot_id", "seq", "tick", "t_dev_ms", "ts_ms", "ts_src", "n_frames", "radar_state",
     "mv_cm", "mv_e", "st_cm", "st_e", "det_cm"]
    + ENERGY_COLUMNS
    + ["light", "out_pin", "ev", "method", "model_version", "pred", "q0", "q1", "q2", "q3", "occ_pm", "p_state",
       "p_decision"]
)

LABEL_UNLABELED = -1
LABEL_CONFLICT = -2


@dataclass
class LabelInterval:
    start_ms: int
    end_ms: int
    label: str


@dataclass
class SessionMeta:
    session_id: str
    device_id: str
    room_id: str
    kind: str  # collect | baseline | event | simulated
    source: str  # device | replay | simulated
    started_at_ms: int
    ended_at_ms: int | None
    samples_path: Path
    labels: list[LabelInterval]
    split: str | None = None
    notes: str = ""
    radar: dict = field(default_factory=dict)

    @property
    def simulated(self) -> bool:
        return self.source == "simulated"

    @property
    def day(self) -> str:
        return pd.Timestamp(self.started_at_ms, unit="ms", tz="UTC").tz_convert("Asia/Ho_Chi_Minh").strftime(
            "%Y-%m-%d"
        )


@dataclass
class Dataset:
    root: Path
    sessions: list[SessionMeta]
    baselines: list[dict]
    info: dict


def load_dataset(path: str | Path) -> Dataset:
    p = Path(path)
    if p.is_dir():
        p = p / "dataset.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    root = p.parent
    sessions = []
    for s in d.get("sessions", []):
        sp = Path(s["samples_path"])
        sessions.append(
            SessionMeta(
                session_id=s["session_id"],
                device_id=s.get("device_id", ""),
                room_id=s.get("room_id", "") or "",
                kind=s.get("kind", "collect"),
                source=s.get("source", "device"),
                started_at_ms=int(s.get("started_at_ms") or 0),
                ended_at_ms=s.get("ended_at_ms"),
                samples_path=sp if sp.is_absolute() else root / sp,
                labels=[LabelInterval(int(a["start_ms"]), int(a["end_ms"]), a["label"]) for a in s.get("labels", [])],
                split=s.get("split"),
                notes=s.get("notes", ""),
                radar=s.get("radar") or {},
            )
        )
    return Dataset(root=root, sessions=sessions, baselines=d.get("baselines", []), info=d.get("info", {}))


def read_samples(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in ("boot_id", "tick", "ts_ms", *ENERGY_COLUMNS) if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: thiếu cột {missing}")
    return df


@dataclass
class Segment:
    """Chuỗi ô liên tục của một lần khởi động trong một phiên (chỉ số = tick - tick0)."""

    session_id: str
    boot_id: int
    tick0: int
    energy: np.ndarray  # (T, 18) uint8, 0 ở ô thiếu
    valid: np.ndarray  # (T,) bool
    ts_ms: np.ndarray  # (T,) int64, ước lượng cho ô thiếu
    label: np.ndarray  # (T,) int: lớp, -1 chưa gán nhãn, -2 xung đột
    device: pd.DataFrame | None = None  # cột ev/method/q*/occ_pm theo ô (để kiểm tra nhất quán)

    @property
    def n_slots(self) -> int:
        return len(self.valid)


def label_slots(ts_ms: np.ndarray, labels: list[LabelInterval]) -> np.ndarray:
    out = np.full(len(ts_ms), LABEL_UNLABELED, dtype=np.int64)
    for iv in labels:
        if iv.label not in C.CLASSES:
            continue
        k = C.CLASSES.index(iv.label)
        m = (ts_ms >= iv.start_ms) & (ts_ms < iv.end_ms)
        conflict = m & (out >= 0) & (out != k)
        out[m & (out == LABEL_UNLABELED)] = k
        out[conflict] = LABEL_CONFLICT
    return out


def build_segments(meta: SessionMeta, df: pd.DataFrame, stats: dict | None = None) -> list[Segment]:
    segs = []
    for boot_id, g in df.groupby("boot_id", sort=False):
        g = g.sort_values(["tick", "seq"] if "seq" in g.columns else ["tick"])
        dup = g.duplicated("tick", keep="first")
        if stats is not None:
            stats["duplicate_ticks"] = stats.get("duplicate_ticks", 0) + int(dup.sum())
        g = g[~dup]
        ticks = g["tick"].to_numpy(np.int64)
        t0 = int(ticks[0])
        T = int(ticks[-1] - t0 + 1)
        idx = ticks - t0
        energy = np.zeros((T, C.N_CHANNELS), dtype=np.uint8)
        e = g[ENERGY_COLUMNS].to_numpy()
        if np.any((e < 0) | (e > C.ENERGY_MAX)):
            if stats is not None:
                stats["energy_out_of_range"] = stats.get("energy_out_of_range", 0) + int(
                    np.any((e < 0) | (e > C.ENERGY_MAX), axis=1).sum())
            e = np.clip(e, 0, C.ENERGY_MAX)
        energy[idx] = e.astype(np.uint8)
        valid = np.zeros(T, dtype=bool)
        valid[idx] = True
        # Thời gian của ô thiếu: nội suy tuyến tính theo tick từ các ô có mẫu (chỉ để gán nhãn, không tạo mẫu)
        ts_known = g["ts_ms"].to_numpy(np.float64)
        ts = np.interp(np.arange(T), idx, ts_known).astype(np.int64)
        label = label_slots(ts, meta.labels)
        dev = None
        if "ev" in g.columns:
            cols = [c for c in ("ev", "method", "model_version", "pred", "q0", "q1", "q2", "q3", "occ_pm", "p_state",
                                "p_decision") if c in g.columns]
            dev = pd.DataFrame(index=np.arange(T), columns=cols, dtype="float64")
            dev.loc[idx, cols] = g[cols].to_numpy(dtype=np.float64)
        segs.append(Segment(meta.session_id, int(boot_id), t0, energy, valid, ts, label, dev))
    return segs


def session_quality(meta: SessionMeta, df: pd.DataFrame, segs: list[Segment], stats: dict) -> dict:
    """Tốc độ thực tế, tỉ lệ thiếu, mất gói, nhãn của một phiên."""
    n = len(df)
    slots = sum(s.n_slots for s in segs)
    valid = sum(int(s.valid.sum()) for s in segs)
    span_s = sum((s.ts_ms[-1] - s.ts_ms[0]) / 1000 + C.SAMPLE_PERIOD_MS / 1000 for s in segs)
    seq_gaps = 0
    if "seq" in df.columns:
        for _, g in df.groupby("boot_id"):
            sq = np.sort(g["seq"].to_numpy(np.int64))
            seq_gaps += int(np.clip(np.diff(sq) - 1, 0, None).sum())
    labeled = sum(int((s.label >= 0).sum()) for s in segs)
    conflict = sum(int((s.label == LABEL_CONFLICT).sum()) for s in segs)
    per_class = {c: 0.0 for c in C.CLASSES}
    for s in segs:
        for k, c in enumerate(C.CLASSES):
            per_class[c] += float((s.label == k).sum()) * C.SAMPLE_PERIOD_MS / 1000
    frames = float(df["n_frames"].mean()) if "n_frames" in df.columns and n else float("nan")
    q = {
        "session_id": meta.session_id,
        "device_id": meta.device_id,
        "room_id": meta.room_id,
        "source": meta.source,
        "kind": meta.kind,
        "samples": n,
        "segments": len(segs),
        "duration_s": round(span_s, 1),
        "sample_rate_hz": round(valid / span_s, 3) if span_s > 0 else 0.0,
        "missing_slot_fraction": round(1 - valid / slots, 4) if slots else 1.0,
        "radar_frames_per_sample": round(frames, 2),
        "radar_rate_hz_est": round(frames * 1000 / C.SAMPLE_PERIOD_MS, 2) if frames == frames else None,
        "seq_gaps": seq_gaps,
        "duplicate_ticks": stats.get("duplicate_ticks", 0),
        "energy_out_of_range": stats.get("energy_out_of_range", 0),
        "labeled_fraction": round(labeled / slots, 4) if slots else 0.0,
        "label_conflict_slots": conflict,
        "class_seconds": {k: round(v, 1) for k, v in per_class.items()},
        "warnings": [],
    }
    w = q["warnings"]
    target = 1000 / C.SAMPLE_PERIOD_MS
    if span_s > 0 and abs(q["sample_rate_hz"] - target) > 0.1 * target:
        w.append(f"tốc độ mẫu thực tế {q['sample_rate_hz']} Hz lệch quá 10% so với {target} Hz")
    if q["missing_slot_fraction"] > 0.05:
        w.append(f"thiếu {q['missing_slot_fraction']:.1%} số ô thời gian")
    if seq_gaps:
        w.append(f"mất {seq_gaps} mẫu trên đường truyền/bộ đệm (khoảng trống seq)")
    if q["duplicate_ticks"]:
        w.append(f"{q['duplicate_ticks']} tick trùng (đã bỏ bản sau)")
    if conflict:
        w.append(f"{conflict} ô có nhãn chồng chéo mâu thuẫn (bị loại)")
    if q["labeled_fraction"] < 0.5:
        w.append(f"chỉ {q['labeled_fraction']:.0%} thời gian có nhãn")
    return q


def load_session(meta: SessionMeta) -> tuple[list[Segment], dict]:
    df = read_samples(meta.samples_path)
    stats: dict = {}
    segs = build_segments(meta, df, stats) if len(df) else []
    return segs, session_quality(meta, df, segs, stats)
