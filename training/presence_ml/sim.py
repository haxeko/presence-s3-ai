"""Bộ mô phỏng radar LD2410 CHỈ để kiểm tra pipeline (không dùng để tuyên bố khả năng phát hiện thực tế).

Sinh khung Engineering Mode ~10 Hz (có dao động thời gian, mất khung, byte hỏng) -> bộ phân tích LD2410
(Python) -> bộ gom mẫu 5 Hz -> các dòng mẫu giống thiết bị gửi lên. Mỗi phiên có kịch bản các đoạn
(nhãn + cảnh: nền phòng, quạt, rèm, người ngồi yên, người di chuyển).

Mô hình năng lượng ở đây là giả định đơn giản của người viết, KHÔNG phải đặc tính đo được của LD2410.
Mọi phiên sinh ra có ``source = "simulated"``; báo cáo đánh giá sẽ ghi rõ "DỮ LIỆU MÔ PHỎNG".

    python -m presence_ml.sim --out data/sim --minutes 120 --seed 1
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import contract as C
from . import ld2410
from .dataio import SAMPLE_COLUMNS
from .sampler import Sampler

G = C.GATES


# --------------------------------------------------------------------------------------------
# Nguồn năng lượng
# --------------------------------------------------------------------------------------------

@dataclass
class Source:
    def energies(self, t: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError


@dataclass
class RoomBackground(Source):
    """Nhiễu nền và vật phản xạ tĩnh (đồ đạc) của một phòng."""

    furniture: dict[int, float] = field(default_factory=lambda: {1: 12.0, 3: 8.0})
    noise: float = 1.0

    def energies(self, t, rng):
        mv = np.clip(rng.normal([9, 6, 4, 3, 3, 2, 2, 2, 2], 2.0 * self.noise), 0, None)
        st = np.clip(rng.normal([6, 5, 3, 2, 2, 2, 1, 1, 1], 1.5 * self.noise), 0, None)
        for g, e in self.furniture.items():
            st[g] = max(st[g], rng.normal(e, 1.5))
        return mv, st


@dataclass
class Fan(Source):
    gate: int = 3
    strength: float = 30.0
    hz: float = 1.3

    def energies(self, t, rng):
        mv = np.zeros(G)
        st = np.zeros(G)
        a = self.strength * (0.55 + 0.45 * abs(np.sin(2 * np.pi * self.hz * t))) + rng.normal(0, 3)
        for d, f in ((0, 1.0), (-1, 0.45), (1, 0.45)):
            g = self.gate + d
            if 0 <= g < G:
                mv[g] = max(mv[g], a * f)
                st[g] = max(st[g], 0.35 * a * f + rng.normal(0, 2))
        return mv, st


@dataclass
class Curtain(Source):
    gate: int = 2
    strength: float = 35.0
    rate_per_min: float = 4.0
    _until: float = -1.0
    _amp: float = 0.0

    def energies(self, t, rng):
        mv = np.zeros(G)
        st = np.zeros(G)
        if t >= self._until and rng.random() < self.rate_per_min / 600:  # gọi ~10 lần/giây
            self._until = t + rng.uniform(1.0, 4.0)
            self._amp = self.strength * rng.uniform(0.5, 1.2)
        if t < self._until:
            a = self._amp * rng.uniform(0.6, 1.0)
            mv[self.gate] = a
            if self.gate + 1 < G:
                mv[self.gate + 1] = 0.4 * a
            st[self.gate] = 0.3 * a
        return mv, st


@dataclass
class PersonStill(Source):
    gate: int = 2
    strength: float = 55.0
    breath_hz: float = 0.25
    _until: float = -1.0

    def energies(self, t, rng):
        mv = np.zeros(G)
        st = np.zeros(G)
        breath = 4.0 * np.sin(2 * np.pi * self.breath_hz * t)
        s = self.strength + breath + rng.normal(0, 3)
        for d, f in ((0, 1.0), (-1, 0.5), (1, 0.5)):
            g = self.gate + d
            if 0 <= g < G:
                st[g] = max(st[g], s * f)
                mv[g] = max(mv[g], (6 + 4 * abs(breath) / 4) * f + rng.normal(0, 2))
        # Thỉnh thoảng đổi tư thế
        if t >= self._until and rng.random() < 1 / 400:
            self._until = t + rng.uniform(0.5, 2.0)
        if t < self._until:
            mv[self.gate] = max(mv[self.gate], rng.uniform(30, 60))
        return mv, st


@dataclass
class PersonMoving(Source):
    speed_gates_s: float = 0.8
    lo: int = 1
    hi: int = 7
    _pos: float = 3.0
    _dir: float = 1.0
    _last: float = 0.0

    def energies(self, t, rng):
        dt = max(0.0, t - self._last)
        self._last = t
        self._pos += self._dir * self.speed_gates_s * dt
        if self._pos > self.hi or self._pos < self.lo or rng.random() < 0.01:
            self._dir = -self._dir
            self._pos = float(np.clip(self._pos, self.lo, self.hi))
        mv = np.zeros(G)
        st = np.zeros(G)
        g0 = int(round(self._pos))
        a = rng.uniform(55, 100) * (1.0 - 0.04 * g0)
        for d, f in ((0, 1.0), (-1, 0.55), (1, 0.55), (-2, 0.2), (2, 0.2)):
            g = g0 + d
            if 0 <= g < G:
                mv[g] = max(mv[g], a * f)
                st[g] = max(st[g], 0.45 * a * f + rng.normal(0, 3))
        return mv, st


def scene_energies(sources: list[Source], t: float, rng) -> tuple[list[int], list[int]]:
    mv = np.zeros(G)
    st = np.zeros(G)
    for s in sources:
        a, b = s.energies(t, rng)
        mv = np.maximum(mv, a)
        st = np.maximum(st, b)
    return np.clip(np.round(mv), 0, 100).astype(int).tolist(), np.clip(np.round(st), 0, 100).astype(int).tolist()


def frame_for(mv: list[int], st: list[int]) -> bytes:
    state = ld2410.target_state_from_energies(mv, st)
    gm = int(np.argmax(mv))
    gs = int(np.argmax(st[2:])) + 2
    mcm, scm = gm * 75 + 37, gs * 75 + 37
    det = min(mcm if state & 1 else 9999, scm if state & 2 else 9999)
    return ld2410.build_report(mv, st, target_state=state, moving_cm=mcm if state & 1 else 0,
                               still_cm=scm if state & 2 else 0, detect_cm=0 if det == 9999 else det)


# --------------------------------------------------------------------------------------------
# Phiên
# --------------------------------------------------------------------------------------------

@dataclass
class Segment:
    seconds: float
    label: str
    sources: list[Source]
    note: str = ""


def simulate_session(segments: list[Segment], rng: np.random.Generator, start_ms: int, session_id: str,
                     device_id: str, boot_id: int, frame_drop: float = 0.01, corrupt: float = 0.005,
                     radar_gap: tuple[float, float] | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """Trả về (các dòng mẫu, các khoảng nhãn)."""
    parser = ld2410.Parser()
    sampler = Sampler()
    rows = []
    labels = []
    total = sum(s.seconds for s in segments)
    seg_bounds = np.cumsum([0.0] + [s.seconds for s in segments])
    for i, s in enumerate(segments):
        labels.append({"start_ms": int(start_ms + seg_bounds[i] * 1000),
                       "end_ms": int(start_ms + seg_bounds[i + 1] * 1000), "label": s.label})
    t = rng.uniform(0, 0.1)
    tick = 0
    next_tick_t = C.SAMPLE_PERIOD_MS / 1000
    seq = 0
    while next_tick_t <= total:
        # khung đến trước thời điểm đóng tick
        while t < next_tick_t:
            k = min(int(np.searchsorted(seg_bounds, t, side="right") - 1), len(segments) - 1)
            silent = radar_gap is not None and radar_gap[0] <= t < radar_gap[1]
            if not silent and rng.random() >= frame_drop:
                mv, st = scene_energies(segments[k].sources, t, rng)
                data = bytearray(frame_for(mv, st))
                if rng.random() < corrupt:
                    data[int(rng.integers(6, len(data)))] ^= 0x5A
                # luồng byte có thể bị chia nhỏ tùy ý
                cut = int(rng.integers(1, len(data)))
                for part in (bytes(data[:cut]), bytes(data[cut:])):
                    for ev in parser.feed(part):
                        if isinstance(ev, ld2410.Report):
                            sampler.add_frame(ev)
            t += 0.1 + rng.normal(0, 0.008)
        smp = sampler.close_tick(tick, int(next_tick_t * 1000))
        if smp is not None:
            ts = start_ms + int(round(next_tick_t * 1000))
            rows.append([
                session_id, device_id, boot_id, seq, tick, smp.t_ms, ts, "device", smp.n_frames, smp.target_state,
                smp.moving_cm, smp.moving_energy, smp.still_cm, smp.still_energy, smp.detect_cm,
                *smp.moving, *smp.still, smp.light, smp.out_pin, 0, 0, 0, -1, 0, 0, 0, 0, -1, 0, 0,
            ])
            seq += 1
        tick += 1
        next_tick_t = (tick + 1) * C.SAMPLE_PERIOD_MS / 1000
    df = pd.DataFrame(rows, columns=SAMPLE_COLUMNS)
    return df, labels


def _room(rng, kind: int) -> RoomBackground:
    return RoomBackground(furniture={1: 12.0, 3: 8.0} if kind == 0 else {2: 14.0, 5: 6.0}, noise=1.0 + 0.2 * kind)


def default_scenarios(rng: np.random.Generator, minutes: float = 120) -> list[dict]:
    """Bộ phiên: đủ 4 lớp và các tình huống khó, rải trên 2 phòng và nhiều ngày."""
    plans = []
    m = max(1.0, minutes / 120)  # hệ số độ dài

    def seg(sec, label, *src, note=""):
        return Segment(sec * m, label, list(src), note)

    for i in range(8):
        room = i % 2
        bg = _room(rng, room)
        fan_gate = int(rng.integers(2, 6))
        person_gate = int(rng.integers(1, 6))
        far_gate = int(rng.integers(6, 8))
        curtain_gate = int(rng.integers(1, 4))
        scripts = [
            [seg(120, "empty_quiet", bg), seg(90, "occupied_moving", bg, PersonMoving()),
             seg(180, "occupied_still", bg, PersonStill(person_gate)), seg(60, "occupied_moving", bg, PersonMoving()),
             seg(150, "empty_quiet", bg)],
            [seg(150, "empty_disturbance", bg, Fan(fan_gate)), seg(60, "occupied_moving", bg, Fan(fan_gate), PersonMoving()),
             seg(180, "occupied_still", bg, Fan(fan_gate), PersonStill(person_gate), note="người ngồi yên khi quạt chạy"),
             seg(150, "empty_disturbance", bg, Fan(fan_gate), note="người ra khỏi phòng, quạt vẫn chạy")],
            [seg(120, "empty_disturbance", bg, Curtain(curtain_gate)), seg(60, "occupied_moving", bg, PersonMoving()),
             seg(180, "occupied_still", bg, PersonStill(fan_gate, strength=45), Fan(fan_gate, strength=25),
                 note="người và quạt cùng khoảng cách"),
             seg(120, "empty_disturbance", bg, Fan(fan_gate, strength=25))],
            [seg(120, "empty_quiet", bg), seg(60, "occupied_moving", bg, PersonMoving(hi=8)),
             seg(180, "occupied_still", bg, PersonStill(far_gate, strength=30), note="người ở xa"),
             seg(120, "empty_quiet", bg), seg(90, "empty_disturbance", bg, Curtain(curtain_gate), Fan(fan_gate))],
        ]
        plans.append({"room": f"phong-{room + 1}", "device": f"s3-sim00{room + 1}", "day": i // 2,
                      "segments": scripts[i % 4]})
    return plans


def generate_dataset(out: Path, minutes: float = 120, seed: int = 1, start_ms: int = 1_767_225_600_000) -> Path:
    """Ghi dataset.json + sessions/<id>/samples.csv. start_ms mặc định: 2026-01-01 07:00 giờ VN."""
    rng = np.random.default_rng(seed)
    out.mkdir(parents=True, exist_ok=True)
    sessions = []
    for i, plan in enumerate(default_scenarios(rng, minutes)):
        sid = f"sim-{seed}-{i:02d}"
        st = start_ms + plan["day"] * 86_400_000 + i * 3_600_000
        gap = None
        if i == 3:
            gap = (200.0, 215.0)  # radar ngừng gửi 15 s: kiểm tra xử lý thiếu dữ liệu
        df, labels = simulate_session(plan["segments"], rng, st, sid, plan["device"], boot_id=1000 + i,
                                      radar_gap=gap)
        d = out / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        df.to_csv(d / "samples.csv", index=False)
        notes = "; ".join(sorted({s.note for s in plan["segments"] if s.note}))
        sessions.append({
            "session_id": sid, "device_id": plan["device"], "room_id": plan["room"], "kind": "simulated",
            "source": "simulated", "started_at_ms": st, "ended_at_ms": int(df["ts_ms"].max()) if len(df) else st,
            "samples_path": f"sessions/{sid}/samples.csv", "labels": labels, "split": None,
            "notes": "MÔ PHỎNG - chỉ kiểm tra pipeline. " + notes,
            "radar": {"variant": "simulated LD2410C", "gate_resolution_cm": 75, "engineering": True},
        })
    ds = {"info": {"generator": "presence_ml.sim", "seed": seed, "simulated": True,
                   "warning": "Dữ liệu mô phỏng, chỉ để kiểm tra pipeline"}, "sessions": sessions, "baselines": []}
    p = out / "dataset.json"
    p.write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description="Sinh bộ dữ liệu MÔ PHỎNG để kiểm tra pipeline")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--minutes", type=float, default=120, help="tổng thời lượng xấp xỉ")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    p = generate_dataset(a.out, a.minutes, a.seed)
    print("wrote", p)


if __name__ == "__main__":
    main()
