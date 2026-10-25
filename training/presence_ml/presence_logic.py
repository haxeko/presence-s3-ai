"""Logic giữ trạng thái hiện diện, giống ``firmware/main/core/presence_logic.cpp``.

Mỗi bước (1 s, hết một hop) nhận một điểm có người (float32) hoặc None khi không đủ dữ liệu radar.

* Mức bằng chứng: CAO (điểm >= enter), THẤP (điểm <= exit), GIỮA, KHÔNG CÓ (thiếu dữ liệu).
* Quyết định (decision): chưa biết / trống / có người.
  - sang "có người" khi CAO liên tục >= confirm_occupied_ms;
  - sang "trống" khi THẤP liên tục >= confirm_empty_ms;
  - GIỮA hoặc KHÔNG CÓ đặt lại cả hai bộ đếm: không bao giờ kết luận "trống" khi thiếu dữ liệu.
* Trạng thái báo cáo (state), 3 giá trị: có người / trống / chưa chắc.
  "chưa chắc" khi: chưa có quyết định nào, thiếu dữ liệu radar, hoặc đang có bằng chứng ngược chiều chưa đủ
  thời gian xác nhận. Khi chưa chắc, quyết định trước đó được giữ nguyên.

Điểm là *điểm mô hình* (hoặc tỉ lệ mẫu vượt ngưỡng), không phải xác suất đã hiệu chuẩn.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contract import PresenceParams

# Giá trị số giống firmware (cột ps/pd trong dữ liệu mẫu)
STATE_UNCERTAIN = 0
STATE_EMPTY = 1
STATE_OCCUPIED = 2
DECISION_UNKNOWN = 0
DECISION_EMPTY = 1
DECISION_OCCUPIED = 2

STATE_NAMES = {STATE_UNCERTAIN: "uncertain", STATE_EMPTY: "empty", STATE_OCCUPIED: "occupied"}
DECISION_NAMES = {DECISION_UNKNOWN: "unknown", DECISION_EMPTY: "empty", DECISION_OCCUPIED: "occupied"}

LEVEL_NONE, LEVEL_LOW, LEVEL_MID, LEVEL_HIGH = 0, 1, 2, 3


@dataclass
class StepResult:
    state: int
    decision: int
    level: int


class PresenceLogic:
    def __init__(self, params: PresenceParams | None = None, hop_ms: int = 1000) -> None:
        self.params = params or PresenceParams()
        self.params.validate()
        self.hop_ms = hop_ms
        self.reset()

    def reset(self) -> None:
        self.decision = DECISION_UNKNOWN
        self.high_ms = 0
        self.low_ms = 0
        self.state = STATE_UNCERTAIN

    def set_params(self, params: PresenceParams) -> None:
        params.validate()
        self.params = params

    def step(self, score: float | None) -> StepResult:
        p = self.params
        if score is None:
            self.high_ms = 0
            self.low_ms = 0
            self.state = STATE_UNCERTAIN
            return StepResult(self.state, self.decision, LEVEL_NONE)
        s = np.float32(score)
        if s >= np.float32(p.enter):
            level = LEVEL_HIGH
            self.high_ms += self.hop_ms
            self.low_ms = 0
        elif s <= np.float32(p.exit):
            level = LEVEL_LOW
            self.low_ms += self.hop_ms
            self.high_ms = 0
        else:
            level = LEVEL_MID
            self.high_ms = 0
            self.low_ms = 0
        if self.decision != DECISION_OCCUPIED and level == LEVEL_HIGH and self.high_ms >= p.confirm_occupied_ms:
            self.decision = DECISION_OCCUPIED
        elif self.decision != DECISION_EMPTY and level == LEVEL_LOW and self.low_ms >= p.confirm_empty_ms:
            self.decision = DECISION_EMPTY
        if self.decision == DECISION_UNKNOWN:
            self.state = STATE_UNCERTAIN
        elif self.decision == DECISION_OCCUPIED:
            self.state = STATE_UNCERTAIN if self.low_ms > 0 else STATE_OCCUPIED
        else:
            self.state = STATE_UNCERTAIN if self.high_ms > 0 else STATE_EMPTY
        return StepResult(self.state, self.decision, level)


def run_sequence(scores: list[float | None], params: PresenceParams | None = None, hop_ms: int = 1000):
    logic = PresenceLogic(params, hop_ms)
    return [logic.step(s) for s in scores]
