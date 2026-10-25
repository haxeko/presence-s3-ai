"""Dòng lệnh của pipeline.

    python -m presence_ml.cli validate --dataset data/jobs/<id>/dataset.json
    python -m presence_ml.cli train    --dataset <dataset.json> --out <thư mục> [--config c.toml] [--key k.pem]
                                       [--model-version N] [--model-id ID] [--allow-simulated]
    python -m presence_ml.cli simulate --out data/sim [--minutes 120] [--seed 1]
    python -m presence_ml.cli verify   --package model.pkg [--pub pub.pem] [--sha256 HEX]
    python -m presence_ml.cli consistency --dataset <dataset.json> --package model.pkg
    python -m presence_ml.cli fixtures [--out firmware/test/fixtures]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_validate(a) -> int:
    from .config import load_config
    from .train import TrainingError, prepare_data

    cfg = load_config(a.config, {"data": {"include_simulated": a.allow_simulated}})
    try:
        prep = prepare_data(a.dataset, cfg)
    except TrainingError as e:
        print("LỖI:", e)
        return 2
    out = {
        "quality": prep["quality"],
        "split": prep["split"].assignment,
        "split_class_seconds": prep["split"].class_seconds,
        "split_warnings": prep["split"].warnings,
        "windows": {k: {"count": len(v), "rejected": dict(v.rejected)} for k, v in prep["windows"].items()},
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def _cmd_train(a) -> int:
    from .package import load_key
    from .train import TrainingError, run_training

    key = load_key(a.key)
    if a.key and key is None:
        print(f"LỖI: không đọc được khóa ký {a.key}")
        return 2
    try:
        s = run_training(a.dataset, a.out, a.config, model_version=a.model_version, model_id=a.model_id,
                         signing_key=key, overrides={"data": {"include_simulated": True}} if a.allow_simulated else None)
    except TrainingError as e:
        print("PROGRESS " + json.dumps({"stage": "error", "error": str(e)}, ensure_ascii=False), flush=True)
        print("LỖI:", e)
        return 2
    print(json.dumps({"model_id": s["model_id"], "package": s["package"], "conclusive": s["conclusive"],
                      "insufficient_data": s["insufficient_data"]}, ensure_ascii=False, indent=1))
    return 0


def _cmd_simulate(a) -> int:
    from .sim import generate_dataset

    print("wrote", generate_dataset(Path(a.out), a.minutes, a.seed))
    return 0


def _cmd_verify(a) -> int:
    from . import package as P

    data = Path(a.package).read_bytes()
    pub = Path(a.pub).read_bytes() if a.pub else None
    try:
        info = P.verify_package(data, pub, a.sha256)
    except P.PackageError as e:
        print("KHÔNG HỢP LỆ:", e)
        return 2
    c = info.contract()
    print(json.dumps({"model_id": info.model_id, "model_version": info.model_version, "sha256": info.sha256,
                      "size": len(data), "signed": info.sig_alg == P.SIG_ECDSA_P256, "simulated": info.simulated,
                      "fw_api": [info.fw_api_min, info.fw_api_max], "classes": c.classes,
                      "input": [c.input_scale, c.input_zero_point], "output": [c.output_scale, c.output_zero_point]},
                     ensure_ascii=False, indent=1))
    return 0


def _cmd_consistency(a) -> int:
    from .consistency import check_consistency

    r = check_consistency(a.dataset, a.package)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


def _cmd_fixtures(a) -> int:
    from .fixtures import generate, generate_model_fixtures, generate_packages

    out = Path(a.out)
    if a.models:
        for n in generate_model_fixtures(out / "model", out / "packages"):
            print("wrote", out / "model" / n)
        return 0
    for n in generate(out):
        print("wrote", out / n)
    for n in generate_packages(out / "packages"):
        print("wrote", out / "packages" / n)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="presence_ml", description="Pipeline TinyML cảm biến hiện diện LD2410")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="kiểm tra dữ liệu, chia tập, đếm cửa sổ")
    v.add_argument("--dataset", required=True)
    v.add_argument("--config")
    v.add_argument("--allow-simulated", action="store_true")
    v.set_defaults(fn=_cmd_validate)
    t = sub.add_parser("train", help="huấn luyện, lượng tử hóa INT8, đánh giá, đóng gói")
    t.add_argument("--dataset", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--config")
    t.add_argument("--key", help="khóa riêng ECDSA P-256 (PEM) để ký gói")
    t.add_argument("--model-version", type=int)
    t.add_argument("--model-id")
    t.add_argument("--allow-simulated", action="store_true", help="CHỈ để kiểm tra pipeline")
    t.set_defaults(fn=_cmd_train)
    s = sub.add_parser("simulate", help="sinh dữ liệu MÔ PHỎNG để kiểm tra pipeline")
    s.add_argument("--out", required=True)
    s.add_argument("--minutes", type=float, default=120)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=_cmd_simulate)
    p = sub.add_parser("verify", help="kiểm tra gói mô hình như firmware")
    p.add_argument("--package", required=True)
    p.add_argument("--pub")
    p.add_argument("--sha256")
    p.set_defaults(fn=_cmd_verify)
    c = sub.add_parser("consistency", help="so điểm thiết bị đã gửi với kết quả máy tính trên cùng mẫu")
    c.add_argument("--dataset", required=True, help="dataset.json hoặc samples.csv của một phiên")
    c.add_argument("--package", required=True)
    c.set_defaults(fn=_cmd_consistency)
    f = sub.add_parser("fixtures", help="sinh fixture cho test C++ của firmware")
    f.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "firmware/test/fixtures"))
    f.add_argument("--models", action="store_true", help="huấn luyện 2 gói mô hình mô phỏng cho test (cần TF)")
    f.set_defaults(fn=_cmd_fixtures)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
