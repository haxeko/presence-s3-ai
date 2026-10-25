"""Kiểm tra nhất quán thiết bị <-> máy tính trên cùng mẫu đầu vào.

Với các phiên ghi lúc thiết bị chạy mô hình X (cột ``method`` = 3, ``model_version`` = X, ``ev`` = 1),
dựng lại đúng cửa sổ 20 ô từ các mẫu backend đã lưu, chạy đặc trưng + lượng tử hóa + mô hình INT8 của gói X
trên máy tính, rồi so với đầu ra int8 (q0..q3) thiết bị đã gửi. Chỉ so các cửa sổ mà backend có đủ mọi mẫu
thiết bị đã dùng (seq liên tục, và không bắt đầu trước mẫu đầu tiên của phiên), vì thiếu mẫu làm cửa sổ hai bên khác
nhau.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import contract as C
from . import package as P
from .dataio import load_dataset, read_samples
from .threshold import METHOD_MODEL


def check_consistency(dataset_path: str | Path, package_path: str | Path) -> dict:
    from .export import Int8Model

    info = P.parse_package(Path(package_path).read_bytes())
    contract = info.contract()
    tv = P.TestVectorSet.unpack(info.payload[P.SECTION_TESTVEC])
    model = Int8Model(info.payload[P.SECTION_TFLITE])
    if str(dataset_path).lower().endswith(".csv"):
        # Một file samples.csv của một phiên (data/sessions/<id>/samples.csv), có nhãn hay không đều được
        from types import SimpleNamespace

        sessions = [SimpleNamespace(session_id=Path(dataset_path).parent.name, samples_path=Path(dataset_path))]
    else:
        sessions = load_dataset(dataset_path).sessions
    compared = skipped = mismatched = 0
    max_diff = 0
    examples = []
    for s in sessions:
        df = read_samples(s.samples_path)
        if "ev" not in df.columns:
            continue
        for boot, g in df.groupby("boot_id"):
            g = g.sort_values("tick").drop_duplicates("tick")
            ticks = g["tick"].to_numpy(np.int64)
            seqs = g["seq"].to_numpy(np.int64)
            energy = g[list(C.CHANNELS)].to_numpy(np.int64)
            pos = {int(t): i for i, t in enumerate(ticks)}
            evals = g[(g["ev"] == 1) & (g["method"] == METHOD_MODEL) & (g["model_version"] == info.model_version)]
            for _, row in evals.iterrows():
                end = int(row["tick"])
                if end - C.WINDOW_SLOTS + 1 < ticks[0]:
                    # Cửa sổ bắt đầu trước mẫu đầu tiên đã ghi (đầu phiên thu): thiết bị có các ô đó, máy tính thì không
                    skipped += 1
                    continue
                idx = [pos.get(t) for t in range(end - C.WINDOW_SLOTS + 1, end + 1)]
                present = [i for i in idx if i is not None]
                if not present:
                    skipped += 1
                    continue
                lo = present[0] - 1 if present[0] > 0 else present[0]
                if np.any(np.diff(seqs[lo: present[-1] + 1]) != 1):
                    skipped += 1  # thiếu mẫu so với thiết bị
                    continue
                valid = np.array([i is not None for i in idx])
                if not C.window_is_usable(valid, contract.min_valid_slots, contract.max_gap_slots):
                    skipped += 1
                    continue
                win = np.zeros((C.WINDOW_SLOTS, C.N_CHANNELS), dtype=np.int64)
                for k, i in enumerate(idx):
                    if i is not None:
                        win[k] = energy[i]
                f = C.compute_features(win, valid)
                q_in = C.quantize_input(C.normalize(f, contract.feature_mean, contract.feature_std),
                                        contract.input_scale, contract.input_zero_point)
                q_pc = model(q_in[None])[0].astype(int)
                q_dev = np.array([int(row[f"q{k}"]) for k in range(C.N_CLASSES)])
                d = int(np.abs(q_pc - q_dev).max())
                max_diff = max(max_diff, d)
                compared += 1
                if d > tv.tol_output_q:
                    mismatched += 1
                    if len(examples) < 10:
                        examples.append({"session_id": s.session_id, "boot_id": int(boot), "tick": end,
                                         "device": q_dev.tolist(), "pc": q_pc.tolist()})
    return {
        "model_id": info.model_id,
        "model_version": info.model_version,
        "compared_windows": compared,
        "skipped_windows": skipped,
        "mismatched_windows": mismatched,
        "max_abs_output_diff": max_diff,
        "tolerance": tv.tol_output_q,
        "examples": examples,
        "ok": compared > 0 and mismatched == 0,
        "note": "ok=false khi không có cửa sổ nào để so (chưa có dữ liệu thiết bị chạy mô hình này)",
    }
