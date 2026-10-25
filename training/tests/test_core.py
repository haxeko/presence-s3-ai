"""Hợp đồng đặc trưng, LD2410, gom mẫu, logic hiện diện, cửa sổ, chia tập, gói mô hình (không cần TensorFlow)."""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from presence_ml import contract as C
from presence_ml import ld2410
from presence_ml import package as P
from presence_ml.dataio import LABEL_UNLABELED, LabelInterval, Segment, SessionMeta, label_slots
from presence_ml.presence_logic import (DECISION_EMPTY, DECISION_OCCUPIED, DECISION_UNKNOWN, STATE_UNCERTAIN,
                                        PresenceLogic)
from presence_ml.sampler import Sampler
from presence_ml.split import split_sessions
from presence_ml.windows import windows_from_segment

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "firmware" / "test" / "fixtures"


# ---------------------------------------------------------------- features
def _reference_features(x: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Cài đặt độc lập (số hữu tỉ chính xác rồi làm tròn về float32) để đối chiếu."""
    out = np.zeros(C.N_FEATURES, dtype=np.float32)
    vi = np.nonzero(v)[0]
    n = len(vi)
    for c in range(C.N_CHANNELS):
        xs = [int(x[i, c]) for i in vi]
        mean = Fraction(sum(xs), n)
        var = sum((Fraction(a) - mean) ** 2 for a in xs) / n
        pairs = [abs(int(x[i, c]) - int(x[i - 1, c])) for i in range(1, len(v)) if v[i] and v[i - 1]]
        out[0 * 18 + c] = np.float32(float(mean))
        out[1 * 18 + c] = np.float32(np.sqrt(np.float64(float(var))))
        out[2 * 18 + c] = np.float32(max(xs))
        out[3 * 18 + c] = np.float32(float(Fraction(sum(pairs), len(pairs)))) if pairs else np.float32(0)
    return out


def test_features_match_independent_reference():
    rng = np.random.default_rng(3)
    for _ in range(200):
        x = rng.integers(0, 101, size=(20, 18))
        v = rng.random(20) > 0.15
        v[0] = True
        x[~v] = 0
        got = C.compute_features(x, v)
        ref = _reference_features(x, v)
        # mean, max, mad: đúng tuyệt đối; std: cùng giá trị hoặc lệch 1 ulp (làm tròn hai lần ở bản tham chiếu)
        assert np.array_equal(got[:18], ref[:18])
        assert np.array_equal(got[36:], ref[36:])
        assert np.all(np.abs(got[18:36] - ref[18:36]) <= np.spacing(np.maximum(ref[18:36], 1e-30)))


def test_feature_order_and_names():
    assert len(C.FEATURE_NAMES) == 72
    assert C.FEATURE_NAMES[0] == "mean_m0" and C.FEATURE_NAMES[17] == "mean_s8"
    assert C.FEATURE_NAMES[18] == "std_m0" and C.FEATURE_NAMES[71] == "mad_s8"


def test_batch_equals_single():
    rng = np.random.default_rng(5)
    x = rng.integers(0, 101, size=(50, 20, 18))
    v = rng.random((50, 20)) > 0.1
    v[:, 0] = True
    b = C.compute_features_batch(x, v)
    for i in range(50):
        assert np.array_equal(b[i], C.compute_features(x[i], v[i]))


def test_quantize_rounding_matches_lroundf():
    z = np.array([0.5, -0.5, 1.5, -1.5, 0.49999997, -0.49999997, 2.5, 126.6, 1000, -1000], dtype=np.float32)
    q = C.quantize_input(z, 1.0, 0)
    assert q.tolist() == [1, -1, 2, -2, 0, 0, 3, 127, 127, -128]


def test_window_rules():
    v = np.ones(20, bool)
    assert C.window_is_usable(v)
    v[[2, 3, 4]] = False
    assert C.window_is_usable(v)  # 17 hợp lệ, khoảng thiếu 3
    v[5] = False
    assert not C.window_is_usable(v)  # khoảng thiếu 4
    v = np.ones(20, bool)
    v[::5] = False
    assert C.window_is_usable(v)  # 16 hợp lệ
    v[1] = False
    assert not C.window_is_usable(v)  # 15 hợp lệ


def test_contract_block_roundtrip():
    rng = np.random.default_rng(1)
    c = C.ContractBlock(input_scale=0.03, input_zero_point=-5, output_scale=1 / 256, output_zero_point=-128,
                        feature_mean=rng.normal(size=72).astype(np.float32),
                        feature_std=rng.uniform(0.5, 9, size=72).astype(np.float32),
                        calib_x=[0, 0.5, 1], calib_y=[0.1, 0.5, 0.9])
    b = c.pack()
    assert len(b) == C.CONTRACT_BIN_SIZE == 888
    d = C.ContractBlock.unpack(b)
    assert d.input_scale == np.float32(0.03) and d.output_zero_point == -128
    assert np.array_equal(d.feature_std, c.feature_std)
    assert d.classes == C.CLASSES
    d.check_supported()
    assert C.calibrate(0.25, d.calib_x, d.calib_y) == pytest.approx(0.3, abs=1e-6)


# ---------------------------------------------------------------- LD2410 + sampler
def test_parser_split_concat_and_garbage():
    f = ld2410.build_report(list(range(9)), list(range(10, 19)), target_state=3, moving_cm=100)
    b = ld2410.build_report(None, None, target_state=1, moving_cm=50, moving_energy=40, still_energy=0)
    stream = b"\x00\xf4\xf3" + f + b + f[:10]
    p = ld2410.Parser()
    out = []
    for i in range(0, len(stream), 3):
        out += p.feed(stream[i: i + 3])
    assert [type(e).__name__ for e in out] == ["Report", "Report"]
    assert out[0].full_layout and out[0].moving == list(range(9)) and out[0].still == list(range(10, 19))
    assert not out[1].engineering and out[1].moving_energy == 40


def test_parser_never_crashes_on_noise():
    rng = np.random.default_rng(0)
    p = ld2410.Parser()
    noise = bytes(rng.integers(0, 256, size=20000, dtype=np.uint8))
    p.feed(noise)
    f = ld2410.build_report([5] * 9, [6] * 9)
    assert len(p.feed(f + f)) >= 1


def test_sampler_max_and_missing():
    s = Sampler()
    r1 = ld2410.parse_report_payload(ld2410.build_report([10] * 9, [1] * 9)[6:-4])
    r2 = ld2410.parse_report_payload(ld2410.build_report([5] * 8 + [100], [3] * 9, target_state=2)[6:-4])
    s.add_frame(r1)
    s.add_frame(r2)
    out = s.close_tick(0, 200)
    assert out.n_frames == 2 and out.moving[0] == 10 and out.moving[8] == 100 and out.still[0] == 3
    assert out.target_state == 2
    assert s.close_tick(1, 400) is None  # ô trống: không lặp mẫu cũ


# ---------------------------------------------------------------- presence logic
def test_presence_logic_hold_and_uncertain():
    lg = PresenceLogic(C.PresenceParams(0.7, 0.3, 2000, 5000))
    assert lg.step(None).state == STATE_UNCERTAIN
    assert lg.step(0.9).decision == DECISION_UNKNOWN
    assert lg.step(0.9).decision == DECISION_OCCUPIED
    for _ in range(4):
        r = lg.step(0.1)
    assert r.decision == DECISION_OCCUPIED and r.state == STATE_UNCERTAIN
    assert lg.step(0.1).decision == DECISION_EMPTY
    for _ in range(20):
        r = lg.step(None)  # mất dữ liệu: không bao giờ tự thành "trống"/"có người"
    assert r.decision == DECISION_EMPTY and r.state == STATE_UNCERTAIN
    with pytest.raises(ValueError):
        C.PresenceParams(0.3, 0.5, 1000, 5000).validate()


# ---------------------------------------------------------------- windows and split
def _segment(labels: list[int], valid: np.ndarray | None = None) -> Segment:
    T = len(labels)
    return Segment("s1", 1, 0, np.zeros((T, 18), np.uint8), np.ones(T, bool) if valid is None else valid,
                   np.arange(T, dtype=np.int64) * 200, np.asarray(labels, dtype=np.int64))


def test_windows_purity_transition_missing():
    labels = [0] * 100 + [2] * 100
    ws = windows_from_segment(_segment(labels), transition_guard_slots=10)
    assert len(ws) > 0
    ends = ws.end_tick
    # Không cửa sổ nào chạm vùng chuyển nhãn (ô 90..109)
    assert all(e < 90 or e - 19 >= 110 for e in ends)
    assert ws.rejected["mixed"] > 0 or ws.rejected["transition"] > 0
    labels = [0] * 60 + [LABEL_UNLABELED] * 40
    ws = windows_from_segment(_segment(labels), transition_guard_slots=0)
    assert all(e < 60 for e in ws.end_tick) and ws.rejected["unlabeled"] > 0
    v = np.ones(100, bool)
    v[40:45] = False
    ws = windows_from_segment(_segment([1] * 100, v), transition_guard_slots=0)
    assert ws.rejected["missing"] > 0
    assert all(not (e - 19 <= 44 and e >= 40) for e in ws.end_tick)


def test_label_slots_conflict_and_unlabeled():
    ts = np.arange(0, 2000, 200)
    lab = label_slots(ts, [LabelInterval(0, 1000, "empty_quiet"), LabelInterval(800, 1400, "occupied_still")])
    assert lab[0] == 0 and lab[4] == -2 and lab[5] == 2 and lab[7] == -1


def _meta(sid, device="d1", day_ms=0, split=None):
    return SessionMeta(sid, device, "r", "collect", "device", day_ms, None, Path("x"), [], split)


def test_split_by_session_and_day_no_leak():
    sessions = [_meta(f"s{i}", day_ms=(i // 2) * 86_400_000) for i in range(10)]
    secs = {s.session_id: {c: 100.0 for c in C.CLASSES} for s in sessions}
    r = split_sessions(sessions, secs, mode="session", seed=1)
    assert set(r.assignment.values()) == {"train", "val", "test"}
    r = split_sessions(sessions, secs, mode="day", seed=1)
    by_day = {}
    for s in sessions:
        by_day.setdefault(r.group_of[s.session_id], set()).add(r.assignment[s.session_id])
    assert all(len(v) == 1 for v in by_day.values())
    fixed = [_meta("a", split="test"), _meta("b"), _meta("c"), _meta("d")]
    r = split_sessions(fixed, {s.session_id: {"empty_quiet": 50.0} for s in fixed})
    assert r.assignment["a"] == "test"


# ---------------------------------------------------------------- package
def test_package_roundtrip_sign_and_tamper():
    priv, pub = P.generate_keypair()
    rng = np.random.default_rng(2)
    c = C.ContractBlock(input_scale=0.05, input_zero_point=0, output_scale=1 / 256, output_zero_point=-128,
                        feature_mean=np.zeros(72, np.float32), feature_std=np.ones(72, np.float32))
    wins = rng.integers(0, 101, size=(3, 20, 18))
    vals = np.ones((3, 20), bool)
    tv = P.make_test_vectors(wins, vals, c, lambda q: np.zeros((len(q), 4), np.int8))
    pkg = P.build_package("m-test", 7, c.pack(), b"\x10" * 100, tv.pack(), {"x": 1}, private_key_pem=priv)
    info = P.verify_package(pkg, pub, hashlib.sha256(pkg).hexdigest())
    assert info.model_version == 7 and info.manifest()["x"] == 1
    assert len(P.TestVectorSet.unpack(info.payload[P.SECTION_TESTVEC]).vectors) == 3
    bad = bytearray(pkg)
    bad[25] ^= 1  # fw_api: phần header được ký
    with pytest.raises(P.PackageError):
        P.verify_package(bytes(bad), pub)
    bad = bytearray(pkg)
    bad[info.sections[P.SECTION_TFLITE][0]] ^= 1
    with pytest.raises(P.PackageError, match="section_hash"):
        P.verify_package(bytes(bad), pub)
    with pytest.raises(P.PackageError, match="sha256"):
        P.verify_package(pkg, pub, "00" * 32)
    other_priv, other_pub = P.generate_keypair()
    with pytest.raises(P.PackageError, match="signature"):
        P.verify_package(pkg, other_pub)


def test_committed_fixtures_are_up_to_date(tmp_path):
    from presence_ml.fixtures import generate

    files = generate(tmp_path)
    for name in files:
        assert (FIX / name).read_text(encoding="utf-8") == (tmp_path / name).read_text(encoding="utf-8"), (
            f"{name} lỗi thời: chạy `python -m presence_ml.cli fixtures`")
    for name in ("pkg_v1.bin", "pkg_v6_fw_api_2.bin"):
        P.parse_package((FIX / "packages" / name).read_bytes())
