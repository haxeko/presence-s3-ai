"""Toàn bộ pipeline trên CPU: kiểm tra dữ liệu -> chia tập -> cửa sổ/đặc trưng -> huấn luyện -> INT8 ->
đánh giá bằng phát lại chuỗi thời gian -> gói mô hình.

    python -m presence_ml.cli train --dataset <dataset.json> --out <thư mục> [--config my.toml] [--key k.pem]

Tiến trình in ra stdout dạng ``PROGRESS {json}`` (backend đọc để hiện trên dashboard).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

from . import contract as C
from . import evaluate as E
from . import package as P
from .config import load_config
from .dataio import Segment, load_dataset, load_session
from .pipeline import Method, float_scorer, int8_scorer, replay, threshold_scorer
from .split import SPLITS, split_sessions
from .threshold import THRESHOLD_PRESENCE_DEFAULTS, factory_thresholds, thresholds_from_baseline
from .windows import WindowSet, windows_from_segment


class TrainingError(Exception):
    pass


def progress(stage: str, **kw) -> None:
    print("PROGRESS " + json.dumps({"stage": stage, **kw}, ensure_ascii=False), flush=True)


def _presence_params(cfg: dict) -> C.PresenceParams:
    p = cfg["presence"]
    return C.PresenceParams(enter=float(p["enter"]), exit=float(p["exit"]),
                            confirm_occupied_ms=int(round(float(p["confirm_occupied_s"]) * 1000)),
                            confirm_empty_ms=int(round(float(p["confirm_empty_s"]) * 1000)))


def prepare_data(dataset_path: str | Path, cfg: dict) -> dict:
    """Đọc + kiểm tra + chia tập + tạo cửa sổ. Không cần TensorFlow."""
    ds = load_dataset(dataset_path)
    dcfg = cfg["data"]
    sessions = [s for s in ds.sessions if s.kind not in set(dcfg.get("exclude_kinds", []))]
    simulated = [s for s in sessions if s.simulated]
    if simulated and not dcfg.get("include_simulated", False):
        sessions = [s for s in sessions if not s.simulated]
    if not sessions:
        raise TrainingError("không có phiên nào để huấn luyện (dữ liệu mô phỏng bị loại trừ khi include_simulated=false)")
    progress("validate", sessions=len(sessions))
    quality = []
    segments: dict[str, list[Segment]] = {}
    class_sec: dict[str, dict[str, float]] = {}
    for s in sessions:
        segs, q = load_session(s)
        segments[s.session_id] = segs
        quality.append(q)
        class_sec[s.session_id] = q["class_seconds"]
    sp = split_sessions(sessions, class_sec, mode=dcfg["split_mode"], ratios=tuple(dcfg["ratios"]),
                        seed=int(dcfg["seed"]))
    guard = int(round(float(dcfg["transition_guard_s"]) * 1000 / C.SAMPLE_PERIOD_MS))
    win: dict[str, WindowSet] = {}
    for name in SPLITS:
        parts = []
        for s in sessions:
            if sp.assignment[s.session_id] != name:
                continue
            for seg in segments[s.session_id]:
                parts.append(windows_from_segment(seg, guard, int(dcfg["min_valid_slots"]), int(dcfg["max_gap_slots"])))
        win[name] = WindowSet.concat(parts)
    # Không cửa sổ nào của một phiên nằm ở hai tập
    for a in SPLITS:
        for b in SPLITS:
            if a < b:
                inter = set(win[a].session) & set(win[b].session)
                if inter:
                    raise TrainingError(f"rò rỉ phiên giữa {a} và {b}: {sorted(inter)}")
    return {"dataset": ds, "sessions": sessions, "segments": segments, "quality": quality, "split": sp,
            "windows": win, "simulated": bool(simulated) and dcfg.get("include_simulated", False)}


def _baseline_thresholds(ds, device_ids: set[str]):
    """Phiên nền đang dùng (active) của thiết bị trong tập dữ liệu, nếu có (cho phương pháp đối chứng)."""
    for b in ds.baselines:
        if b.get("active") and b.get("device_id") in device_ids and b.get("stats"):
            return thresholds_from_baseline(b["stats"]), b
    return None, None


def sufficiency(prep: dict, cfg: dict, test_quality: dict) -> list[str]:
    """Lý do chưa đủ dữ liệu để kết luận (rỗng = đủ theo ngưỡng cấu hình)."""
    ecfg = cfg["evaluation"]
    reasons = []
    sp = prep["split"]
    test_groups = {g for g, s in sp.groups.items() if s == "test"}
    for c in C.CLASSES:
        minutes = sp.class_seconds["test"][c] / 60
        if minutes < float(ecfg["min_test_minutes_per_class"]):
            reasons.append(f"tập test chỉ có {minutes:.1f} phút lớp {c} (cần >= {ecfg['min_test_minutes_per_class']})")
        groups = {sp.group_of[q["session_id"]] for q in prep["quality"]
                  if sp.group_of[q["session_id"]] in test_groups and q["class_seconds"][c] > 0}
        if len(groups) < int(ecfg["min_test_groups_per_class"]):
            reasons.append(f"lớp {c} chỉ có trong {len(groups)} nhóm (phiên/ngày) của tập test "
                           f"(cần >= {ecfg['min_test_groups_per_class']})")
    if test_quality.get("empty_hours", 0) < float(ecfg["min_empty_hours"]):
        reasons.append(f"tập test có {test_quality.get('empty_hours', 0):.2f} giờ phòng trống "
                       f"(cần >= {ecfg['min_empty_hours']} để ước lượng số báo giả mỗi giờ)")
    if prep["simulated"]:
        reasons.insert(0, "DỮ LIỆU MÔ PHỎNG: chỉ kiểm tra pipeline, không phản ánh khả năng phát hiện thực tế")
    return reasons


def run_training(dataset_path: str | Path, out_dir: str | Path, config_path: str | Path | None = None,
                 model_version: int | None = None, model_id: str | None = None,
                 signing_key: bytes | None = None, overrides: dict | None = None) -> dict:
    import keras

    t_start = time.time()
    cfg = load_config(config_path, overrides)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_version = int(model_version or int(time.time()))
    model_id = model_id or time.strftime("m%Y%m%d-%H%M%S", time.gmtime())
    prep = prepare_data(dataset_path, cfg)
    win = prep["windows"]
    for name in SPLITS:
        progress("windows", split=name, windows=len(win[name]), rejected=dict(win[name].rejected))
    tr, va, te = win["train"], win["val"], win["test"]
    if len(tr) == 0 or len(np.unique(tr.label)) < 2:
        raise TrainingError("tập train cần cửa sổ của ít nhất 2 lớp (thu thêm và gán nhãn dữ liệu)")
    if len(va) == 0:
        raise TrainingError("tập validation rỗng: cần ít nhất 3 phiên (hoặc 3 ngày) có nhãn")

    f_tr = C.compute_features_batch(tr.samples, tr.valid)
    f_va = C.compute_features_batch(va.samples, va.valid)
    f_te = C.compute_features_batch(te.samples, te.valid) if len(te) else np.zeros((0, C.N_FEATURES), np.float32)
    mean, std = C.fit_normalization(f_tr)  # chỉ từ train
    z_tr, z_va = C.normalize(f_tr, mean, std), C.normalize(f_va, mean, std)

    from .model import build_model, class_weights

    mcfg = cfg["model"]
    model = build_model(list(mcfg["hidden"]), float(mcfg["learning_rate"]), int(mcfg["seed"]))
    cw = class_weights(tr.label, mcfg["class_weight"])
    epochs = int(mcfg["epochs"])

    class Progress(keras.callbacks.Callback):
        def on_epoch_end(self, epoch, logs=None):
            logs = logs or {}
            progress("train", epoch=epoch + 1, epochs=epochs,
                     **{k: round(float(v), 5) for k, v in logs.items()})

    best_path = out / "model_best.keras"
    hist = model.fit(
        z_tr, tr.label, validation_data=(z_va, va.label), epochs=epochs, batch_size=int(mcfg["batch_size"]),
        class_weight=cw, verbose=0, shuffle=True,
        callbacks=[
            keras.callbacks.EarlyStopping(monitor="val_loss", patience=int(mcfg["patience"]),
                                          restore_best_weights=True),
            keras.callbacks.ModelCheckpoint(str(best_path), monitor="val_loss", save_best_only=True),
            Progress(),
        ],
    )
    model = keras.models.load_model(best_path)
    history = {k: [round(float(x), 5) for x in v] for k, v in hist.history.items()}
    best_epoch = int(np.argmin(history["val_loss"])) + 1

    # ---- INT8 ----
    progress("quantize")
    from .export import Int8Model, convert_int8

    ecfg = cfg["export"]
    rng = np.random.default_rng(int(mcfg["seed"]))
    n_rep = min(int(ecfg["representative_samples"]), len(z_tr))
    rep = z_tr[rng.choice(len(z_tr), size=n_rep, replace=False)]
    tflite = convert_int8(model, rep)
    int8 = Int8Model(tflite)
    (out / "model.tflite").write_bytes(tflite)

    def predict_float(z):
        return model.predict(z, verbose=0, batch_size=1024)

    # ---- đánh giá theo cửa sổ (float và INT8) ----
    progress("evaluate")
    contract_tmp = C.ContractBlock(
        input_scale=float(int8.input_scale), input_zero_point=int(int8.input_zero_point),
        output_scale=float(int8.output_scale), output_zero_point=int(int8.output_zero_point),
        feature_mean=mean, feature_std=std, arena_bytes=int(ecfg["arena_bytes"]), presence=_presence_params(cfg),
        min_valid_slots=int(cfg["data"]["min_valid_slots"]), max_gap_slots=int(cfg["data"]["max_gap_slots"]),
    )
    contract_tmp.check_supported()

    def int8_outputs(feats):
        q_in = C.quantize_input(C.normalize(feats, mean, std), contract_tmp.input_scale, contract_tmp.input_zero_point)
        return int8(q_in)

    report_windows = {}
    quant_cmp = {}
    for name, ws, feats in (("val", va, f_va), ("test", te, f_te)):
        if len(ws) == 0:
            continue
        pf = predict_float(C.normalize(feats, mean, std))
        qo = int8_outputs(feats)
        report_windows[name] = {"float": E.window_metrics(ws.label, np.argmax(pf, axis=1)),
                                "int8": E.window_metrics(ws.label, np.argmax(qo, axis=1))}
        quant_cmp[name] = E.quantization_comparison(pf, qo, contract_tmp.output_scale,
                                                    contract_tmp.output_zero_point, ws.label)

    # ---- hiệu chuẩn điểm (validation) ----
    va_scores = C.occupied_score_from_q(int8_outputs(f_va), contract_tmp.output_scale, contract_tmp.output_zero_point)
    va_occ = np.isin(va.label, C.OCCUPIED_CLASSES)
    calib = E.calibration_table(va_scores, va_occ)
    contract_blk = C.ContractBlock(**{**vars(contract_tmp), "calib_x": calib.get("x", []), "calib_y": calib.get("y", [])})
    contract_blk.check_supported()

    # ---- phát lại chuỗi thời gian tập test qua toàn bộ logic ----
    progress("replay")
    test_ids = [s.session_id for s in prep["sessions"] if prep["split"].assignment[s.session_id] == "test"]
    test_segments = [seg for sid in test_ids for seg in prep["segments"][sid]]
    devices = {s.device_id for s in prep["sessions"] if s.session_id in test_ids}
    base_thr, base_info = _baseline_thresholds(prep["dataset"], devices)
    params = _presence_params(cfg)
    methods = [
        Method("int8", int8_scorer(contract_blk, int8), params),
        Method("float", float_scorer(mean, std, predict_float), params),
        Method("threshold_factory", threshold_scorer(factory_thresholds()), THRESHOLD_PRESENCE_DEFAULTS),
    ]
    if base_thr is not None:
        methods.append(Method("threshold_baseline", threshold_scorer(base_thr), THRESHOLD_PRESENCE_DEFAULTS))
    ecfg_eval = cfg["evaluation"]
    timeline = {}
    rp = replay(test_segments, methods, min_valid=contract_blk.min_valid_slots, max_gap=contract_blk.max_gap_slots)
    if len(rp):
        rp.to_csv(out / "replay_test.csv", index=False)
        for m in methods:
            timeline[m.name] = E.timeline_metrics(rp, m.name, hop_s=C.HOP_SLOTS * C.SAMPLE_PERIOD_MS / 1000,
                                                  entry_cap_s=float(ecfg_eval["entry_latency_cap_s"]),
                                                  exit_cap_s=float(ecfg_eval["exit_latency_cap_s"]))
    empty_hours = (timeline.get("int8", {}).get("empty_s") or 0) / 3600
    reasons = sufficiency(prep, cfg, {"empty_hours": empty_hours})
    test_scores = rp["int8_score"].to_numpy() if len(rp) else np.zeros(0)
    usable_lab = (rp["usable"] & (rp["truth"] >= 0)).to_numpy() if len(rp) else np.zeros(0, bool)
    occ_truth = np.isin(rp["truth"].to_numpy(), C.OCCUPIED_CLASSES) if len(rp) else np.zeros(0, bool)
    calib_eval = {
        "ece_raw_score_test": E.ece(test_scores[usable_lab], occ_truth[usable_lab]) if usable_lab.any() else None,
        "ece_calibrated_test": E.ece(
            np.array([C.calibrate(float(s), contract_blk.calib_x, contract_blk.calib_y) for s in test_scores[usable_lab]]),
            occ_truth[usable_lab]) if usable_lab.any() and calib else None,
        "table": calib,
        "note": "Điểm mô hình (tổng softmax các lớp có người) KHÔNG phải xác suất. Bảng hiệu chuẩn học trên "
                "validation, chỉ đúng khi dữ liệu thực tế giống dữ liệu validation.",
    }

    # ---- vector kiểm tra + gói ----
    progress("package")
    tv_src = te if len(te) else va
    n_tv = min(int(ecfg["test_vectors"]), len(tv_src))
    pick = np.linspace(0, len(tv_src) - 1, n_tv).astype(int) if n_tv else np.zeros(0, int)
    tv = P.make_test_vectors(tv_src.samples[pick], tv_src.valid[pick], contract_blk, int8)
    tv.tol_input_q = int(ecfg["tol_input_q"])
    tv.tol_output_q = int(ecfg["tol_output_q"])
    flags = P.FLAG_SIMULATED if prep["simulated"] else 0
    summary = {
        "model_id": model_id,
        "model_version": model_version,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "simulated_data": prep["simulated"],
        "insufficient_data": reasons,
        "conclusive": not reasons,
        "contract": C.contract_summary(),
        "architecture": {"input": C.N_FEATURES, "hidden": list(mcfg["hidden"]), "output": C.N_CLASSES,
                         "activation": "relu", "output_activation": "softmax"},
        "quantization": {"input_scale": contract_blk.input_scale, "input_zero_point": contract_blk.input_zero_point,
                         "output_scale": contract_blk.output_scale, "output_zero_point": contract_blk.output_zero_point,
                         "representative_samples": n_rep, "source": "train windows", "ops": int8.ops()},
        "normalization": {"mean": [float(x) for x in mean], "std": [float(x) for x in std], "fit_on": "train"},
        "presence_defaults": vars(params),
        "training": {"best_epoch": best_epoch, "epochs_run": len(history["loss"]), "class_weight": cw,
                     "history": history},
        "data": {
            "quality": prep["quality"],
            "split_mode": cfg["data"]["split_mode"],
            "split": prep["split"].groups,
            "session_split": prep["split"].assignment,
            "split_class_seconds": prep["split"].class_seconds,
            "split_warnings": prep["split"].warnings,
            "windows": {n: {"count": len(win[n]), "per_class": np.bincount(win[n].label, minlength=C.N_CLASSES).tolist(),
                            "rejected": dict(win[n].rejected)} for n in SPLITS},
        },
        "evaluation": {
            "windows": report_windows,
            "quantization": quant_cmp,
            "timeline": timeline,
            "timeline_methods": [m.name for m in methods],
            "baseline": {k: base_info.get(k) for k in ("baseline_id", "name", "condition")} if base_info else None,
            "calibration": calib_eval,
        },
        "config": cfg,
        "tflite": {"size": len(tflite), "sha256": hashlib.sha256(tflite).hexdigest()},
    }
    pkg = P.build_package(model_id, model_version, contract_blk.pack(), tflite, tv.pack(),
                          {k: summary[k] for k in ("model_id", "model_version", "created_at", "simulated_data",
                                                   "conclusive", "contract", "architecture", "quantization",
                                                   "presence_defaults")}
                          | {"classes": C.CLASSES, "evaluation_summary": _short_eval(summary)},
                          flags=flags, private_key_pem=signing_key)
    info = P.verify_package(pkg, P.public_from_private(signing_key) if signing_key else None)
    (out / "model.pkg").write_bytes(pkg)
    summary["package"] = {"file": "model.pkg", "size": len(pkg), "sha256": info.sha256, "signed": bool(signing_key),
                          "test_vectors": n_tv, "flags": flags}
    summary["duration_s"] = round(time.time() - t_start, 1)
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=_json_default),
                                     encoding="utf-8")
    from .report import render_markdown

    (out / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    progress("done", model_id=model_id, model_version=model_version, conclusive=not reasons)
    return summary


def _short_eval(summary: dict) -> dict:
    ev = summary["evaluation"]
    tl = ev["timeline"].get("int8", {})
    w = ev["windows"].get("test", {}).get("int8", {})
    return {"test_window_accuracy_int8": w.get("accuracy"),
            "false_alarms_per_hour": tl.get("false_alarms_per_hour"),
            "missed_s": tl.get("missed_s"), "longest_miss_s": tl.get("longest_miss_s"),
            "insufficient_data": summary["insufficient_data"]}


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


if __name__ == "__main__":
    sys.exit("dùng: python -m presence_ml.cli train ...")
