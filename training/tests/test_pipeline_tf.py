"""Pipeline đầy đủ với TensorFlow trên dữ liệu MÔ PHỎNG (kiểm tra pipeline, không phải độ chính xác thực tế)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

tf = pytest.importorskip("tensorflow")

from presence_ml import contract as C  # noqa: E402
from presence_ml import package as P  # noqa: E402
from presence_ml.consistency import check_consistency  # noqa: E402
from presence_ml.dataio import load_dataset  # noqa: E402
from presence_ml.export import Int8Model  # noqa: E402
from presence_ml.sim import generate_dataset  # noqa: E402
from presence_ml.threshold import METHOD_MODEL  # noqa: E402
from presence_ml.train import TrainingError, run_training  # noqa: E402


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    d = tmp_path_factory.mktemp("pipe")
    ds = generate_dataset(d / "sim", minutes=45, seed=4)
    priv, pub = P.generate_keypair()
    out = d / "model"
    summary = run_training(ds, out, model_version=3, model_id="t-3", signing_key=priv,
                           overrides={"data": {"include_simulated": True}, "model": {"epochs": 40}})
    return {"dir": out, "summary": summary, "dataset": ds, "pub": pub}


def test_simulated_data_is_refused_by_default(tmp_path):
    ds = generate_dataset(tmp_path / "sim", minutes=10, seed=2)
    with pytest.raises(TrainingError, match="mô phỏng"):
        run_training(ds, tmp_path / "m")


def test_report_flags_simulated_and_insufficient(trained):
    s = trained["summary"]
    assert s["simulated_data"] is True and s["conclusive"] is False
    assert any("MÔ PHỎNG" in r for r in s["insufficient_data"])
    rep = json.loads((trained["dir"] / "report.json").read_text(encoding="utf-8"))
    assert set(rep["evaluation"]["timeline"]) >= {"int8", "float", "threshold_factory"}
    assert "CHƯA ĐỦ DỮ LIỆU" in (trained["dir"] / "report.md").read_text(encoding="utf-8")


def test_no_session_in_two_splits(trained):
    split = trained["summary"]["data"]["session_split"]
    assert set(split.values()) <= {"train", "val", "test"}
    assert len(split) == len(load_dataset(trained["dataset"]).sessions)


def test_package_int8_and_vectors(trained):
    pkg = (trained["dir"] / "model.pkg").read_bytes()
    info = P.verify_package(pkg, trained["pub"])
    assert info.simulated
    m = Int8Model(info.payload[P.SECTION_TFLITE])
    c = info.contract()
    assert m.input_scale == pytest.approx(c.input_scale) and m.output_zero_point == c.output_zero_point
    tv = P.TestVectorSet.unpack(info.payload[P.SECTION_TESTVEC])
    for v in tv.vectors:
        f = C.compute_features(v.energy, v.valid)
        assert np.array_equal(f, v.features)
        q = C.quantize_input(C.normalize(f, c.feature_mean, c.feature_std), c.input_scale, c.input_zero_point)
        assert np.array_equal(q, v.input_q)
        assert np.array_equal(m(v.input_q[None])[0], v.output_q)
    ops = set(m.ops())
    assert ops <= {"FULLY_CONNECTED", "SOFTMAX", "RESHAPE", "QUANTIZE", "DEQUANTIZE"}, ops
    q = trained["summary"]["evaluation"]["quantization"]
    assert q["test"]["argmax_agreement"] >= 0.9


def test_consistency_check_detects_mismatch(trained, tmp_path):
    """Giả lập thiết bị: ghi q0..q3 do Python tính vào CSV, rồi so; sửa một giá trị -> phát hiện lệch."""
    pkg_path = trained["dir"] / "model.pkg"
    info = P.parse_package(pkg_path.read_bytes())
    c = info.contract()
    m = Int8Model(info.payload[P.SECTION_TFLITE])
    ds = load_dataset(trained["dataset"])
    s = ds.sessions[0]
    df = pd.read_csv(s.samples_path).head(400).copy()
    df["ev"] = 0
    df["method"] = 0
    ticks = df["tick"].to_numpy()
    e = df[list(C.CHANNELS)].to_numpy()
    for i, t in enumerate(ticks):
        if (t + 1) % 5 or i < 19 or t - ticks[i - 19] != 19:
            continue
        f = C.compute_features(e[i - 19: i + 1], np.ones(20, bool))
        q = C.quantize_input(C.normalize(f, c.feature_mean, c.feature_std), c.input_scale, c.input_zero_point)
        out = m(q[None])[0]
        df.loc[df.index[i], ["ev", "method", "model_version", "q0", "q1", "q2", "q3"]] = [
            1, METHOD_MODEL, info.model_version, *[int(x) for x in out]]
    sp = tmp_path / "s.csv"
    df.to_csv(sp, index=False)
    dsj = {"sessions": [{"session_id": "c1", "device_id": "d", "samples_path": str(sp), "labels": []}]}
    (tmp_path / "dataset.json").write_text(json.dumps(dsj), encoding="utf-8")
    r = check_consistency(tmp_path / "dataset.json", pkg_path)
    assert r["ok"] and r["compared_windows"] > 10 and r["max_abs_output_diff"] == 0
    # Phiên thu bắt đầu giữa chừng: cửa sổ đầu của thiết bị có ô trước mẫu đầu tiên của CSV -> bỏ qua, không báo lệch
    late = tmp_path / "late.csv"
    df.iloc[3:].to_csv(late, index=False)
    r = check_consistency(late, pkg_path)
    assert r["ok"] and r["skipped_windows"] >= 1 and r["mismatched_windows"] == 0
    j = df.index[df["ev"] == 1][3]
    df.loc[j, "q0"] = int(df.loc[j, "q0"]) + 9 if int(df.loc[j, "q0"]) < 100 else int(df.loc[j, "q0"]) - 9
    df.to_csv(sp, index=False)
    r = check_consistency(tmp_path / "dataset.json", pkg_path)
    assert not r["ok"] and r["mismatched_windows"] == 1
