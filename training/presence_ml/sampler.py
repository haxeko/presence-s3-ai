"""Gom khung radar (~10 Hz) thành mẫu 5 Hz, giống ``firmware/main/core/sampler.cpp``.

Tick k đóng ô thời gian [T_k - 200 ms, T_k). Mẫu của tick k:
* năng lượng 18 kênh = giá trị LỚN NHẤT của từng kênh trên các khung Engineering đủ 9+9 gate trong ô,
  chặn trên 100;
* trạng thái, khoảng cách, byte phụ = của khung hợp lệ CUỐI CÙNG trong ô;
* n_frames = số khung hợp lệ trong ô.
Ô không có khung hợp lệ => không có mẫu (thiếu dữ liệu). KHÔNG lặp lại mẫu cũ để lấp chỗ trống.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import ENERGY_MAX, GATES
from .ld2410 import Report


@dataclass
class Sample:
    tick: int
    t_ms: int
    n_frames: int
    target_state: int
    moving_cm: int
    moving_energy: int
    still_cm: int
    still_energy: int
    detect_cm: int
    moving: list[int]
    still: list[int]
    light: int = -1
    out_pin: int = -1

    @property
    def energies(self) -> list[int]:
        return self.moving + self.still


class Sampler:
    def __init__(self) -> None:
        self._reset()
        self.frames_total = 0
        self.frames_rejected = 0  # khung không đủ 9+9 gate hoặc không phải Engineering
        self.empty_ticks = 0

    def _reset(self) -> None:
        self._count = 0
        self._moving = [0] * GATES
        self._still = [0] * GATES
        self._last: Report | None = None

    def add_frame(self, r: Report) -> bool:
        self.frames_total += 1
        if not r.full_layout:
            self.frames_rejected += 1
            return False
        for g in range(GATES):
            self._moving[g] = max(self._moving[g], min(r.moving[g], ENERGY_MAX))
            self._still[g] = max(self._still[g], min(r.still[g], ENERGY_MAX))
        self._count += 1
        self._last = r
        return True

    def close_tick(self, tick: int, t_ms: int) -> Sample | None:
        if self._count == 0:
            self.empty_ticks += 1
            self._reset()
            return None
        r = self._last
        assert r is not None
        s = Sample(
            tick=tick,
            t_ms=t_ms,
            n_frames=self._count,
            target_state=r.target_state,
            moving_cm=r.moving_cm,
            moving_energy=r.moving_energy,
            still_cm=r.still_cm,
            still_energy=r.still_energy,
            detect_cm=r.detect_cm,
            moving=list(self._moving),
            still=list(self._still),
            light=r.light,
            out_pin=r.out_pin,
        )
        self._reset()
        return s
