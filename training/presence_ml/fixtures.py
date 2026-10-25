"""Sinh file fixture dạng văn bản cho test C++ của firmware (so khớp Python <-> C++).

    python -m presence_ml.fixtures --out firmware/test/fixtures

* ld2410_frames.txt: luồng byte (có khung bị chia, nối tiếp, hỏng) và các khung Python giải mã được;
* features.txt: cửa sổ 20 ô (có ô thiếu) và 72 đặc trưng float32 (dạng hex bit) kỳ vọng;
* presence.txt: chuỗi điểm và trạng thái/ quyết định kỳ vọng của logic giữ trạng thái;
* threshold.txt: cửa sổ và điểm phương pháp ngưỡng kỳ vọng.
Test Python ``tests/test_fixtures.py`` kiểm tra các file đã commit khớp với mã hiện tại.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np

from . import contract as C
from . import ld2410
from .presence_logic import PresenceLogic
from .threshold import Thresholds, factory_thresholds, window_score


def f32hex(x: float) -> str:
    return struct.pack("<f", np.float32(x)).hex()


def _frames_fixture(rng: np.random.Generator) -> str:
    lines = ["# ld2410 parser fixture: 'stream <hex>' then expected 'report ...' / 'ack ...' lines, 'end'"]
    cases = []
    eng = lambda m, s, **kw: ld2410.build_report(m, s, **kw)  # noqa: E731
    m1 = [10, 20, 30, 40, 50, 60, 70, 80, 90]
    s1 = [1, 2, 3, 4, 5, 6, 7, 8, 100]
    f_eng = eng(m1, s1, target_state=3, moving_cm=120, still_cm=85, detect_cm=130)
    f_basic = ld2410.build_report(None, None, target_state=1, moving_cm=200, still_cm=0, detect_cm=210,
                                  moving_energy=55, still_energy=0)
    f_ack = ld2410.build_ack(ld2410.CMD_ENABLE_CONFIG, 0, bytes([0x01, 0x00, 0x40, 0x00]))
    f_short = eng([5, 6, 7], [8, 9, 10], target_state=0)  # N=M=2: hợp lệ nhưng không đủ 9 gate
    cases.append(("single engineering frame", f_eng))
    cases.append(("basic + engineering + ack back to back", f_basic + f_eng + f_ack))
    cases.append(("garbage before and between", bytes([0x00, 0xF4, 0xF3, 0x11, 0xFD]) + f_eng + bytes([0xF4, 0xF4]) + f_basic))
    bad_footer = bytearray(f_eng)
    bad_footer[-1] = 0x00
    cases.append(("bad footer dropped, next read", bytes(bad_footer) + f_basic))
    bad_tail = bytearray(f_eng)
    bad_tail[-6] = 0x56
    cases.append(("bad tail 0x55 dropped", bytes(bad_tail) + f_eng))
    bad_check = bytearray(f_eng)
    bad_check[-5] = 0x01
    cases.append(("bad check byte dropped", bytes(bad_check) + f_basic))
    bad_head = bytearray(f_eng)
    bad_head[7] = 0xAB
    cases.append(("bad head 0xAA dropped", bytes(bad_head) + f_basic))
    cases.append(("length above 64 dropped", bytes([0xF4, 0xF3, 0xF2, 0xF1, 0x41, 0x00]) + f_eng))
    cut = f_eng[:-8]
    cases.append(("truncated frame then two frames", cut + f_basic + f_eng))
    # Khung thật nằm bên trong một khung có LEN bị hỏng (LEN lớn hơn thực tế)
    corrupt_len = bytearray(f_basic)
    corrupt_len[4] = 0x30
    cases.append(("frame hidden inside corrupt length", bytes(corrupt_len) + f_eng + f_basic))
    cases.append(("engineering with 3 gates", f_short + f_eng))
    gates_too_many = bytearray(eng(m1 + [1], s1 + [1]))
    cases.append(("engineering with 10 gates dropped", bytes(gates_too_many) + f_basic))
    cases.append(("states 4-6", eng(m1, s1, target_state=4) + eng(m1, s1, target_state=6)))
    # Luồng ngẫu nhiên dài: khung hợp lệ xen nhiễu
    stream = bytearray()
    for _ in range(40):
        if rng.random() < 0.3:
            stream += bytes(rng.integers(0, 256, size=int(rng.integers(1, 12)), dtype=np.uint8))
        m = rng.integers(0, 101, size=9).tolist()
        s = rng.integers(0, 101, size=9).tolist()
        stream += eng(m, s, target_state=int(rng.integers(0, 4)), moving_cm=int(rng.integers(0, 600)),
                      still_cm=int(rng.integers(0, 600)), detect_cm=int(rng.integers(0, 600)))
    cases.append(("random stream", bytes(stream)))

    for name, data in cases:
        p = ld2410.Parser()
        events = p.feed(data)
        lines.append(f"case {name}")
        lines.append(f"stream {data.hex()}")
        # Chia luồng thành nhiều đoạn ngẫu nhiên: kết quả phải giống nhau
        chunks = []
        pos = 0
        while pos < len(data):
            n = int(rng.integers(1, 9))
            chunks.append(n)
            pos += n
        lines.append("chunks " + " ".join(str(c) for c in chunks))
        for e in events:
            if isinstance(e, ld2410.Report):
                lines.append(
                    "report {} {} {} {} {} {} {} {} {} {} {} {} {}".format(
                        e.type, e.target_state, e.moving_cm, e.moving_energy, e.still_cm, e.still_energy,
                        e.detect_cm, int(e.engineering), e.max_moving_gate, e.max_still_gate,
                        ",".join(map(str, e.moving)) or "-", ",".join(map(str, e.still)) or "-",
                        e.aux.hex() or "-",
                    )
                )
            else:
                lines.append(f"ack {e.command} {e.status} {e.data.hex() or '-'}")
        st = p.stats
        lines.append(f"stats {st.reports} {st.acks} {st.bad_length} {st.bad_footer} {st.bad_payload}")
        lines.append("end")
    return "\n".join(lines) + "\n"


def _random_window(rng: np.random.Generator, kind: str) -> tuple[np.ndarray, np.ndarray]:
    W = C.WINDOW_SLOTS
    x = np.zeros((W, C.N_CHANNELS), dtype=np.int64)
    valid = np.ones(W, dtype=bool)
    if kind == "zeros":
        pass
    elif kind == "max":
        x[:] = 100
    elif kind == "random":
        x = rng.integers(0, 101, size=(W, C.N_CHANNELS))
    elif kind == "smooth":
        base = rng.integers(0, 60, size=C.N_CHANNELS)
        x = np.clip(base + rng.integers(-5, 6, size=(W, C.N_CHANNELS)), 0, 100)
    elif kind == "missing":
        x = rng.integers(0, 101, size=(W, C.N_CHANNELS))
        valid[rng.choice(W, size=int(rng.integers(1, 5)), replace=False)] = False
    elif kind == "alternate":
        x = rng.integers(0, 101, size=(W, C.N_CHANNELS))
        valid[1::2] = False  # không có cặp kề nhau: mad = 0
    elif kind == "single":
        x = rng.integers(0, 101, size=(W, C.N_CHANNELS))
        valid[:] = False
        valid[7] = True
    x[~valid] = 0
    return x.astype(np.int64), valid


def _features_fixture(rng: np.random.Generator) -> str:
    lines = [
        "# features fixture: 'window <name>' then 20 lines 'v e0..e17' (v=1 valid), then 'features <72 float32 hex LE>'",
        "# usable <0|1>: quy tắc cửa sổ (>= 16 ô hợp lệ, khoảng thiếu <= 3 ô)",
    ]
    kinds = ["zeros", "max", "single", "alternate"] + ["random"] * 6 + ["smooth"] * 6 + ["missing"] * 8
    for i, kind in enumerate(kinds):
        x, v = _random_window(rng, kind)
        f = C.compute_features(x, v)
        lines.append(f"window {i}_{kind}")
        for row, ok in zip(x, v):
            lines.append(f"{int(ok)} " + " ".join(str(int(e)) for e in row))
        lines.append("usable " + str(int(C.window_is_usable(v))))
        lines.append("features " + " ".join(f32hex(val) for val in f))
    # Chuẩn hóa + lượng tử hóa: dùng cửa sổ cuối và tham số giả định
    mean = rng.uniform(0, 50, size=C.N_FEATURES).astype(np.float32)
    std = rng.uniform(0.5, 20, size=C.N_FEATURES).astype(np.float32)
    scale, zp = 0.0473, -7
    lines.append("normalization " + " ".join(f32hex(m) for m in mean) + " | " + " ".join(f32hex(s) for s in std))
    lines.append(f"quant {f32hex(scale)} {zp}")
    for i in range(6):
        x, v = _random_window(rng, "random" if i % 2 else "smooth")
        f = C.compute_features(x, v)
        z = C.normalize(f, mean, std)
        q = C.quantize_input(z, scale, zp)
        lines.append("qcase " + " ".join(f32hex(val) for val in f) + " | " + " ".join(str(int(a)) for a in q))
    # Trường hợp làm tròn biên .5
    edge = np.array([0.5, -0.5, 1.5, -1.5, 2.4999998, 0.49999997, 126.5, -128.5, 300.0, -300.0, 0.0],
                    dtype=np.float32)
    for e_scale, e_zp in ((1.0, 0), (1.0, -3), (0.25, 5)):
        q = C.quantize_input(edge, e_scale, e_zp)
        lines.append(
            f"qedge {f32hex(e_scale)} {e_zp} " + " ".join(f32hex(e) for e in edge) + " | "
            + " ".join(str(int(a)) for a in q)
        )
    return "\n".join(lines) + "\n"


def _presence_fixture(rng: np.random.Generator) -> str:
    lines = ["# presence fixture: 'params enter exit confirm_occ_ms confirm_empty_ms' then 'step <score|none> state decision'"]
    scenarios = []
    seq = [None] * 3 + [0.9] * 5 + [0.5] * 3 + [0.1] * 70 + [0.95, 0.2, 0.95, 0.95] + [None] * 5 + [0.1] * 61
    scenarios.append((C.PresenceParams(0.7, 0.3, 2000, 60000), seq))
    scenarios.append((C.PresenceParams(0.6, 0.4, 1000, 30000), [float(v) for v in rng.uniform(0, 1, size=200)]))
    scenarios.append((C.PresenceParams(0.7, 0.3, 0, 5000), [0.7, 0.3, 0.3, 0.3, 0.3, 0.3, 0.70000005, 0.29999998]))
    for params, scores in scenarios:
        lines.append(
            f"params {f32hex(params.enter)} {f32hex(params.exit)} {params.confirm_occupied_ms} {params.confirm_empty_ms}"
        )
        logic = PresenceLogic(params)
        for s in scores:
            r = logic.step(s)
            lines.append(f"step {'none' if s is None else f32hex(s)} {r.state} {r.decision}")
        lines.append("end")
    return "\n".join(lines) + "\n"


def _threshold_fixture(rng: np.random.Generator) -> str:
    lines = ["# threshold fixture: 'thr m0..m8 s0..s8', 'window' + 20 rows like features.txt, 'score <f32 hex>'"]
    for thr in (factory_thresholds(), Thresholds(moving=[20] * 9, still=[100, 100, 15, 15, 15, 15, 15, 15, 15])):
        lines.append("thr " + " ".join(map(str, thr.moving + thr.still)))
        for kind in ("random", "smooth", "missing", "zeros"):
            x, v = _random_window(rng, kind)
            lines.append(f"window {kind}")
            for row, ok in zip(x, v):
                lines.append(f"{int(ok)} " + " ".join(str(int(e)) for e in row))
            lines.append("score " + f32hex(window_score(x, v, thr)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------
# Gói mô hình giả cho test logic A/B của firmware (engine giả, không cần TFLM)
# --------------------------------------------------------------------------------------------
# "Mô hình giả": blob tflite = b"FAKEMODEL\0" + <f i f i> (thang/điểm 0 vào/ra); đầu ra lớp k =
# clamp(sum(in[18k:18k+18]) >> 4, -128, 127). test/host/fake_engine.h làm đúng như vậy.
FAKE_MAGIC = b"FAKEMODEL\0"


def fake_infer(q_in: np.ndarray) -> np.ndarray:
    q = np.asarray(q_in, dtype=np.int32).reshape(-1, C.N_FEATURES)
    s = q.reshape(-1, C.N_CLASSES, C.N_FEATURES // C.N_CLASSES).sum(axis=2)
    return np.clip(s >> 4, -128, 127).astype(np.int8)


def _fake_contract(rng: np.random.Generator, **kw) -> C.ContractBlock:
    mean = rng.uniform(0, 40, size=C.N_FEATURES).astype(np.float32)
    std = rng.uniform(1, 15, size=C.N_FEATURES).astype(np.float32)
    args = dict(input_scale=0.05, input_zero_point=-3, output_scale=1 / 256, output_zero_point=-128,
                feature_mean=mean, feature_std=std, arena_bytes=4096,
                calib_x=[0.0, 0.5, 1.0], calib_y=[0.02, 0.4, 0.97])
    args.update(kw)
    return C.ContractBlock(**args)


def _fake_package(rng, version: int, key: bytes | None, *, contract=None, blob_quant=None, corrupt_vectors=False,
                  fw_api=(1, 1), flags=0) -> bytes:
    from . import package as P

    c = contract or _fake_contract(rng)
    q = blob_quant or (c.input_scale, c.input_zero_point, c.output_scale, c.output_zero_point)
    blob = FAKE_MAGIC + struct.pack("<fifi", *q) + bytes(rng.integers(0, 256, size=64, dtype=np.uint8))
    wins = np.stack([_random_window(rng, k)[0] for k in ("random", "smooth", "missing", "smooth", "random", "missing")])
    vals = np.stack([np.ones(C.WINDOW_SLOTS, bool)] * len(wins))
    for i in range(len(wins)):
        vals[i] = wins[i].sum(axis=1) >= 0  # tất cả hợp lệ ...
    vals[2, [3, 9]] = False  # ... trừ vài ô của cửa sổ "missing"
    vals[5, [0, 1, 17]] = False
    wins[~vals] = 0
    tv = P.make_test_vectors(wins, vals, c, fake_infer)
    if corrupt_vectors:
        tv.vectors[1].output_q = (tv.vectors[1].output_q.astype(np.int16) + 5).clip(-128, 127).astype(np.int8)
    manifest = {"model_id": f"fake-v{version}", "model_version": version, "note": "host test fixture"}
    return P.build_package(f"fake-v{version}", version, c.pack(), blob, tv.pack(), manifest, fw_api=fw_api,
                           flags=flags, private_key_pem=key)


def generate_packages(out_dir: Path, seed: int = 7) -> list[str]:
    from . import package as P

    rng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Khóa CHỈ để kiểm thử; khóa riêng không được commit (.gitignore), mất thì tạo mới cùng toàn bộ gói giả
    key_path = out_dir / "test_signing_key.pem"
    if not key_path.exists():
        priv, pub = P.generate_keypair()
        key_path.write_bytes(priv)
        (out_dir / "test_signing_pub.pem").write_bytes(pub)
        other_priv, _ = P.generate_keypair()
        (out_dir / "other_signing_key.pem").write_bytes(other_priv)
    key = key_path.read_bytes()
    other = (out_dir / "other_signing_key.pem").read_bytes()
    pkgs = {
        "pkg_v1.bin": _fake_package(rng, 1, key),
        "pkg_v2.bin": _fake_package(rng, 2, key),
        "pkg_v3_other_key.bin": _fake_package(rng, 3, other),
        "pkg_v4_unsigned.bin": _fake_package(rng, 4, None),
        "pkg_v5_bad_vectors.bin": _fake_package(rng, 5, key, corrupt_vectors=True),
        "pkg_v6_fw_api_2.bin": _fake_package(rng, 6, key, fw_api=(2, 3)),
        "pkg_v7_res20.bin": _fake_package(rng, 7, key, contract=_fake_contract(rng, gate_resolution_cm=20)),
        "pkg_v8_quant_mismatch.bin": _fake_package(rng, 8, key, blob_quant=(0.07, -3, 1 / 256, -128)),
        "pkg_v9_simulated.bin": _fake_package(rng, 9, key, flags=P.FLAG_SIMULATED),
    }
    lines = ["# name version size sha256"]
    for name, data in pkgs.items():
        (out_dir / name).write_bytes(data)
        info = P.parse_package(data)
        lines.append(f"{name} {info.model_version} {len(data)} {info.sha256}")
    (out_dir / "packages.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return list(pkgs)


def generate_model_fixtures(out_dir: Path, key_dir: Path) -> list[str]:
    """Hai gói mô hình THẬT (TFLite INT8) huấn luyện trên dữ liệu MÔ PHỎNG, cho test TFLM trên máy tính và
    test trên ESP32-S3/QEMU. Cần TensorFlow. Chỉ để kiểm thử, không dùng trên thiết bị thật."""
    import shutil
    import tempfile

    from .sim import generate_dataset
    from .train import run_training

    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / "test_signing_key.pem"
    if not key_path.exists():
        raise SystemExit("Thiếu khóa thử nghiệm (không commit): chạy `python -m presence_ml.cli fixtures` trước để "
                         "tạo khóa và gói giả mới, rồi mới chạy `fixtures --models`.")
    key = key_path.read_bytes()
    shutil.copyfile(key_dir / "test_signing_pub.pem", out_dir / "signing_pub.pem")
    names = []
    with tempfile.TemporaryDirectory() as tmp:
        ds = generate_dataset(Path(tmp) / "sim", minutes=60, seed=11)
        for version, seed in ((1, 1), (2, 2)):
            o = Path(tmp) / f"m{version}"
            run_training(ds, o, model_version=version, model_id=f"sim-fixture-v{version}", signing_key=key,
                         overrides={"data": {"include_simulated": True}, "model": {"seed": seed, "epochs": 80}})
            shutil.copyfile(o / "model.pkg", out_dir / f"model_v{version}.pkg")
            names.append(f"model_v{version}.pkg")
    (out_dir / "README.md").write_text(
        "Gói mô hình huấn luyện trên dữ liệu MÔ PHỎNG, ký bằng khóa thử nghiệm của packages/ — chỉ dùng cho "
        "test (TFLM trên máy tính, ESP32-S3/QEMU). Tạo lại: `python -m presence_ml.cli fixtures --models`.\n",
        encoding="utf-8")
    return names


def generate(out_dir: Path, seed: int = 20261025) -> dict[str, str]:
    rng = np.random.default_rng(seed)
    files = {
        "ld2410_frames.txt": _frames_fixture(rng),
        "features.txt": _features_fixture(rng),
        "presence.txt": _presence_fixture(rng),
        "threshold.txt": _threshold_fixture(rng),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "firmware/test/fixtures")
    args = ap.parse_args()
    files = generate(args.out)
    for name in files:
        print("wrote", args.out / name)
    for name in generate_packages(args.out / "packages"):
        print("wrote", args.out / "packages" / name)


if __name__ == "__main__":
    main()
