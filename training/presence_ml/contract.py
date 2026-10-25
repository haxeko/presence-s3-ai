"""Hợp đồng đầu vào v1 (feature set ``energy18_stats4_v1``): dùng chung cho máy tính và ESP32.

Mọi chi tiết ở đây phải giống hệt ``firmware/main/core/features.cpp`` và ``model_contract.cpp``.
Thay đổi bất kỳ điều gì (kênh, thống kê, thứ tự, công thức, cửa sổ, tần số, cách lượng tử hóa)
=> tạo CONTRACT_VERSION / FEATURE_SET_ID mới và huấn luyện lại.

Đầu vào: cửa sổ 20 ô thời gian (4 s ở 5 Hz). Mỗi ô có một mẫu 18 kênh năng lượng
(m0..m8 chuyển động, s0..s8 tĩnh, số nguyên 0..100) hoặc bị thiếu.

Đặc trưng (72 = 4 thống kê x 18 kênh), thứ tự "thống kê trước": chỉ số = stat * 18 + kênh.
Với n ô hợp lệ x_1..x_n của một kênh (tính bằng số nguyên, chỉ một phép chia/căn float32 cuối cùng
nên Python và C++ cho kết quả float32 giống hệt từng bit):

* mean = float32(S1) / float32(n),                S1 = sum x
* std  = sqrt(float32(n*S2 - S1^2) / float32(n*n)), S2 = sum x^2   (độ lệch chuẩn tổng thể)
* max  = float32(max x)
* mad  = float32(D) / float32(P), D = sum |x_i - x_(i-1)| trên P cặp ô kề nhau cùng hợp lệ; P = 0 => 0

Chuẩn hóa: z = (f - mean_k) / std_k (float32, mean/std học từ tập train, std >= STD_FLOOR).
Lượng tử hóa đầu vào int8: q = clamp(round_half_away(z / input_scale) + input_zero_point, -128, 127).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

CONTRACT_VERSION = 1
FEATURE_SET_ID = 1
FEATURE_SET_NAME = "energy18_stats4_v1"

GATES = 9
CHANNELS: list[str] = [f"m{g}" for g in range(GATES)] + [f"s{g}" for g in range(GATES)]
N_CHANNELS = len(CHANNELS)  # 18
STATS: list[str] = ["mean", "std", "max", "mad"]
N_STATS = len(STATS)
FEATURE_NAMES: list[str] = [f"{s}_{c}" for s in STATS for c in CHANNELS]
N_FEATURES = len(FEATURE_NAMES)  # 72

ENERGY_MAX = 100

SAMPLE_PERIOD_MS = 200  # 5 Hz
WINDOW_SLOTS = 20  # 4 s
HOP_SLOTS = 5  # 1 s
MIN_VALID_SLOTS = 16  # cửa sổ cần ít nhất 16/20 ô có dữ liệu
MAX_GAP_SLOTS = 3  # không quá 3 ô liên tiếp bị thiếu (0,6 s)
GATE_RESOLUTION_CM = 75

STD_FLOOR = np.float32(0.5)

CLASSES: list[str] = ["empty_quiet", "empty_disturbance", "occupied_still", "occupied_moving"]
N_CLASSES = len(CLASSES)
OCCUPIED_CLASSES: tuple[int, ...] = (2, 3)
OCCUPIED_MASK = sum(1 << c for c in OCCUPIED_CLASSES)
UNLABELED = "unlabeled"

CLASS_LABELS_VI = {
    "empty_quiet": "Phòng trống, ít nhiễu",
    "empty_disturbance": "Phòng trống, có nhiễu (quạt/rèm)",
    "occupied_still": "Có người ngồi/nằm yên",
    "occupied_moving": "Có người di chuyển",
    UNLABELED: "Chưa gán nhãn",
}


def is_occupied_class(c: int) -> bool:
    return c in OCCUPIED_CLASSES


# --------------------------------------------------------------------------------------------
# Đặc trưng
# --------------------------------------------------------------------------------------------

def window_is_usable(valid: np.ndarray, min_valid: int = MIN_VALID_SLOTS, max_gap: int = MAX_GAP_SLOTS) -> bool:
    """Quy tắc chấp nhận cửa sổ (giống firmware): đủ ô hợp lệ và không có khoảng thiếu quá dài."""
    v = np.asarray(valid, dtype=bool)
    if int(v.sum()) < min_valid:
        return False
    return longest_gap(v) <= max_gap


def longest_gap(valid: np.ndarray) -> int:
    run = best = 0
    for ok in np.asarray(valid, dtype=bool):
        run = 0 if ok else run + 1
        best = max(best, run)
    return best


def compute_features(samples: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """72 đặc trưng float32 của một cửa sổ. samples: (W, 18) số nguyên 0..100, valid: (W,) bool."""
    f = compute_features_batch(np.asarray(samples)[None], np.asarray(valid)[None])
    return f[0]


def compute_features_batch(samples: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Phiên bản vectơ hóa: samples (N, W, 18), valid (N, W) -> (N, 72) float32.

    Mỗi phần tử được tính đúng như compute_features (tổng số nguyên rồi một phép chia/căn float32).
    """
    x = np.asarray(samples).astype(np.int64)
    v = np.asarray(valid).astype(bool)
    if x.ndim != 3 or x.shape[2] != N_CHANNELS or v.shape != x.shape[:2]:
        raise ValueError(f"bad shapes samples={x.shape} valid={v.shape}")
    if np.any((x < 0) | (x > ENERGY_MAX)):
        raise ValueError("energies must be 0..100")
    vm = v[:, :, None]
    n = v.sum(axis=1).astype(np.int64)  # (N,)
    if np.any(n == 0):
        raise ValueError("window without valid samples")
    xv = np.where(vm, x, 0)
    s1 = xv.sum(axis=1)  # (N, 18)
    s2 = (xv * xv).sum(axis=1)
    n_col = n[:, None]
    mean = s1.astype(np.float32) / n_col.astype(np.float32)
    var_num = n_col * s2 - s1 * s1
    std = np.sqrt(var_num.astype(np.float32) / (n_col * n_col).astype(np.float32))
    mx = np.where(vm, x, -1).max(axis=1).astype(np.float32)
    pairs = v[:, 1:] & v[:, :-1]  # (N, W-1)
    p = pairs.sum(axis=1).astype(np.int64)
    d = np.where(pairs[:, :, None], np.abs(x[:, 1:] - x[:, :-1]), 0).sum(axis=1)
    p_col = np.maximum(p, 1)[:, None]
    mad = np.where(p[:, None] > 0, d.astype(np.float32) / p_col.astype(np.float32), np.float32(0))
    out = np.concatenate([mean, std, mx, mad.astype(np.float32)], axis=1)
    return out.astype(np.float32, copy=False)


# --------------------------------------------------------------------------------------------
# Chuẩn hóa và lượng tử hóa
# --------------------------------------------------------------------------------------------

def fit_normalization(train_features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """mean/std (float32) chỉ từ tập train. std được chặn dưới bởi STD_FLOOR."""
    f = np.asarray(train_features, dtype=np.float64)
    mean = f.mean(axis=0).astype(np.float32)
    std = f.std(axis=0).astype(np.float32)
    std = np.maximum(std, STD_FLOOR).astype(np.float32)
    return mean, std


def normalize(features: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    f = np.asarray(features, dtype=np.float32)
    return ((f - mean.astype(np.float32)) / std.astype(np.float32)).astype(np.float32)


def round_half_away(v32: np.ndarray) -> np.ndarray:
    """lroundf của C: làm tròn nửa ra xa 0. Tính trong float64 để v + 0.5 không bị làm tròn."""
    v = np.asarray(v32, dtype=np.float32).astype(np.float64)
    return np.where(v >= 0, np.floor(v + 0.5), -np.floor(-v + 0.5))


def quantize_input(z: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    v = (np.asarray(z, dtype=np.float32) / np.float32(scale)).astype(np.float32)
    q = round_half_away(v) + int(zero_point)
    return np.clip(q, -128, 127).astype(np.int8)


def dequantize_output(q: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    return ((np.asarray(q).astype(np.int32) - int(zero_point)).astype(np.float32) * np.float32(scale)).astype(
        np.float32
    )


def occupied_score_from_q(q_out: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """Điểm có người = tổng điểm của các lớp có người (float32, giống firmware).

    Đây là *điểm mô hình*, không phải xác suất đã hiệu chuẩn.
    """
    q = np.asarray(q_out).astype(np.int32)
    acc = np.zeros(q.shape[:-1], dtype=np.int32)
    for c in OCCUPIED_CLASSES:
        acc = acc + (q[..., c] - int(zero_point))
    return (acc.astype(np.float32) * np.float32(scale)).astype(np.float32)


# --------------------------------------------------------------------------------------------
# Khối hợp đồng nhị phân trong gói mô hình (firmware đọc khối này, không đọc JSON)
# --------------------------------------------------------------------------------------------

CONTRACT_MAGIC = b"PSC1"
CALIB_MAX_POINTS = 16
CLASS_NAME_LEN = 24
# Bố cục (little-endian, mọi trường 4 byte), xem docs/model-package.md:
#   0 magic "PSC1" | 4 contract_version | 8 feature_set_id | 12 feature_count | 16 sample_period_ms
#  20 window_slots | 24 hop_slots | 28 min_valid_slots | 32 max_gap_slots | 36 gate_resolution_cm
#  40 class_count | 44 occupied_mask | 48 arena_bytes | 52 input_scale f32 | 56 input_zero_point i32
#  60 output_scale f32 | 64 output_zero_point i32 | 68 enter f32 | 72 exit f32 | 76 confirm_occupied_ms
#  80 confirm_empty_ms | 84 calib_count | 88 calib_x[16] f32 | 152 calib_y[16] f32
# 216 class_names[4][24] | 312 feature_mean[72] f32 | 600 feature_std[72] f32 | 888 hết
_CONTRACT_HEAD = struct.Struct("<4s12IfififfIII")
CONTRACT_BIN_SIZE = (
    _CONTRACT_HEAD.size + 4 * CALIB_MAX_POINTS * 2 + CLASS_NAME_LEN * N_CLASSES + 4 * N_FEATURES * 2
)


@dataclass
class PresenceParams:
    """Tham số logic giữ trạng thái (giống firmware presence_logic)."""

    enter: float = 0.7
    exit: float = 0.3
    confirm_occupied_ms: int = 2000
    confirm_empty_ms: int = 60000

    def validate(self) -> None:
        if not (0.0 <= self.exit < self.enter <= 1.0):
            raise ValueError("cần 0 <= exit < enter <= 1")
        if not (0 <= self.confirm_occupied_ms <= 600000 and 1000 <= self.confirm_empty_ms <= 3600000):
            raise ValueError("thời gian xác nhận ngoài khoảng cho phép")


@dataclass
class ContractBlock:
    input_scale: float
    input_zero_point: int
    output_scale: float
    output_zero_point: int
    feature_mean: np.ndarray
    feature_std: np.ndarray
    arena_bytes: int = 16384
    presence: PresenceParams = field(default_factory=PresenceParams)
    calib_x: list[float] = field(default_factory=list)
    calib_y: list[float] = field(default_factory=list)
    classes: list[str] = field(default_factory=lambda: list(CLASSES))
    occupied_mask: int = OCCUPIED_MASK
    contract_version: int = CONTRACT_VERSION
    feature_set_id: int = FEATURE_SET_ID
    feature_count: int = N_FEATURES
    sample_period_ms: int = SAMPLE_PERIOD_MS
    window_slots: int = WINDOW_SLOTS
    hop_slots: int = HOP_SLOTS
    min_valid_slots: int = MIN_VALID_SLOTS
    max_gap_slots: int = MAX_GAP_SLOTS
    gate_resolution_cm: int = GATE_RESOLUTION_CM

    def pack(self) -> bytes:
        if len(self.calib_x) != len(self.calib_y) or len(self.calib_x) > CALIB_MAX_POINTS:
            raise ValueError("calibration table too large")
        if len(self.classes) != N_CLASSES:
            raise ValueError("class count")
        head = _CONTRACT_HEAD.pack(
            CONTRACT_MAGIC,
            self.contract_version,
            self.feature_set_id,
            self.feature_count,
            self.sample_period_ms,
            self.window_slots,
            self.hop_slots,
            self.min_valid_slots,
            self.max_gap_slots,
            self.gate_resolution_cm,
            len(self.classes),
            self.occupied_mask,
            self.arena_bytes,
            self.input_scale,
            self.input_zero_point,
            self.output_scale,
            self.output_zero_point,
            self.presence.enter,
            self.presence.exit,
            self.presence.confirm_occupied_ms,
            self.presence.confirm_empty_ms,
            len(self.calib_x),
        )
        cx = list(self.calib_x) + [0.0] * (CALIB_MAX_POINTS - len(self.calib_x))
        cy = list(self.calib_y) + [0.0] * (CALIB_MAX_POINTS - len(self.calib_y))
        names = b"".join(n.encode()[: CLASS_NAME_LEN - 1].ljust(CLASS_NAME_LEN, b"\0") for n in self.classes)
        body = (
            struct.pack(f"<{CALIB_MAX_POINTS}f", *cx)
            + struct.pack(f"<{CALIB_MAX_POINTS}f", *cy)
            + names
            + np.asarray(self.feature_mean, dtype="<f4").tobytes()
            + np.asarray(self.feature_std, dtype="<f4").tobytes()
        )
        out = head + body
        assert len(out) == CONTRACT_BIN_SIZE
        return out

    @classmethod
    def unpack(cls, data: bytes) -> "ContractBlock":
        if len(data) != CONTRACT_BIN_SIZE:
            raise ValueError(f"contract block size {len(data)} != {CONTRACT_BIN_SIZE}")
        h = _CONTRACT_HEAD.unpack_from(data, 0)
        if h[0] != CONTRACT_MAGIC:
            raise ValueError("bad contract magic")
        if h[10] != N_CLASSES:
            raise ValueError(f"class_count {h[10]} != {N_CLASSES}")
        off = _CONTRACT_HEAD.size
        cx = struct.unpack_from(f"<{CALIB_MAX_POINTS}f", data, off)
        off += 4 * CALIB_MAX_POINTS
        cy = struct.unpack_from(f"<{CALIB_MAX_POINTS}f", data, off)
        off += 4 * CALIB_MAX_POINTS
        names = [
            data[off + i * CLASS_NAME_LEN: off + (i + 1) * CLASS_NAME_LEN].split(b"\0")[0].decode()
            for i in range(N_CLASSES)
        ]
        off += CLASS_NAME_LEN * N_CLASSES
        mean = np.frombuffer(data, dtype="<f4", count=N_FEATURES, offset=off).astype(np.float32)
        off += 4 * N_FEATURES
        std = np.frombuffer(data, dtype="<f4", count=N_FEATURES, offset=off).astype(np.float32)
        n_cal = min(h[21], CALIB_MAX_POINTS)
        return cls(
            contract_version=h[1],
            feature_set_id=h[2],
            feature_count=h[3],
            sample_period_ms=h[4],
            window_slots=h[5],
            hop_slots=h[6],
            min_valid_slots=h[7],
            max_gap_slots=h[8],
            gate_resolution_cm=h[9],
            occupied_mask=h[11],
            arena_bytes=h[12],
            input_scale=h[13],
            input_zero_point=h[14],
            output_scale=h[15],
            output_zero_point=h[16],
            presence=PresenceParams(enter=h[17], exit=h[18], confirm_occupied_ms=h[19], confirm_empty_ms=h[20]),
            calib_x=list(cx[:n_cal]),
            calib_y=list(cy[:n_cal]),
            classes=names,
            feature_mean=mean,
            feature_std=std,
        )

    def check_supported(self) -> None:
        """Các kiểm tra firmware v1 thực hiện (giữ giống model_contract.cpp)."""
        problems = []
        if self.contract_version != CONTRACT_VERSION:
            problems.append(f"contract_version {self.contract_version}")
        if self.feature_set_id != FEATURE_SET_ID:
            problems.append(f"feature_set_id {self.feature_set_id}")
        if self.feature_count != N_FEATURES:
            problems.append(f"feature_count {self.feature_count}")
        if self.sample_period_ms != SAMPLE_PERIOD_MS or self.window_slots != WINDOW_SLOTS:
            problems.append("sample period/window")
        if not (1 <= self.hop_slots <= self.window_slots):
            problems.append("hop")
        if not (1 <= self.min_valid_slots <= self.window_slots) or self.max_gap_slots >= self.window_slots:
            problems.append("missing-data rules")
        if self.occupied_mask == 0 or self.occupied_mask >= (1 << N_CLASSES):
            problems.append("occupied_mask")
        if not (self.input_scale > 0 and self.output_scale > 0):
            problems.append("scales")
        if np.any(~np.isfinite(self.feature_mean)) or np.any(~(self.feature_std > 0)):
            problems.append("normalization")
        if problems:
            raise ValueError("unsupported contract: " + ", ".join(problems))


def calibrate(score: float, xs: list[float], ys: list[float]) -> float | None:
    """Nội suy tuyến tính bảng hiệu chuẩn (giống firmware). None nếu không có bảng."""
    if not xs:
        return None
    if score <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if score <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (score - x0) / (x1 - x0)
    return ys[-1]


def contract_summary() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "feature_set": FEATURE_SET_NAME,
        "feature_set_id": FEATURE_SET_ID,
        "channels": CHANNELS,
        "stats": STATS,
        "order": "stat_major: index = stat * 18 + channel",
        "feature_names": FEATURE_NAMES,
        "dtype": "float32",
        "sample_rate_hz": 1000 / SAMPLE_PERIOD_MS,
        "sample_period_ms": SAMPLE_PERIOD_MS,
        "window_s": WINDOW_SLOTS * SAMPLE_PERIOD_MS / 1000,
        "window_slots": WINDOW_SLOTS,
        "hop_s": HOP_SLOTS * SAMPLE_PERIOD_MS / 1000,
        "hop_slots": HOP_SLOTS,
        "min_valid_slots": MIN_VALID_SLOTS,
        "max_gap_slots": MAX_GAP_SLOTS,
        "gate_resolution_cm": GATE_RESOLUTION_CM,
        "std_floor": float(STD_FLOOR),
        "classes": CLASSES,
        "occupied_classes": list(OCCUPIED_CLASSES),
        "input_quantization": "q = clamp(round_half_away(((f - mean) / std) / scale) + zero_point, -128, 127)",
    }
