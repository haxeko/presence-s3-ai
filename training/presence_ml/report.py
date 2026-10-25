"""Báo cáo đánh giá dạng Markdown (tiếng Việt) từ report.json."""

from __future__ import annotations

from . import contract as C

METHOD_VI = {
    "int8": "Mô hình INT8 (chạy trên ESP32)",
    "float": "Mô hình float (trước lượng tử hóa)",
    "threshold_factory": "Ngưỡng xuất xưởng LD2410 + giữ trạng thái",
    "threshold_baseline": "Ngưỡng từ phiên nền + giữ trạng thái",
}


def _fmt(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.{nd}f}"
    return str(v)


def _cm_table(cm: list[list[int]], names: list[str]) -> str:
    head = "| thật \\ dự đoán | " + " | ".join(names) + " |\n|---|" + "---|" * len(names) + "\n"
    rows = "".join(f"| {names[i]} | " + " | ".join(str(x) for x in row) + " |\n" for i, row in enumerate(cm))
    return head + rows


def render_markdown(r: dict) -> str:
    out = []
    out.append(f"# Báo cáo mô hình `{r['model_id']}` (phiên bản {r['model_version']})\n")
    out.append(f"Tạo lúc {r['created_at']} UTC, huấn luyện {r.get('duration_s', '—')} s trên CPU.\n")
    if r["insufficient_data"]:
        out.append("> **CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN.** Các số dưới đây chỉ mô tả dữ liệu hiện có:\n")
        for reason in r["insufficient_data"]:
            out.append(f"> - {reason}")
        out.append("")
    if r["simulated_data"]:
        out.append("> **DỮ LIỆU MÔ PHỎNG** — báo cáo này chỉ chứng minh pipeline chạy được, "
                   "không nói gì về khả năng phát hiện người thật.\n")

    ev = r["evaluation"]
    out.append("## Theo dòng thời gian (phát lại tập test qua mô hình + logic giữ trạng thái)\n")
    tl = ev["timeline"]
    if tl:
        names = [m for m in ev["timeline_methods"] if m in tl]
        out.append("| Chỉ số | " + " | ".join(METHOD_VI.get(m, m) for m in names) + " |")
        out.append("|---|" + "---|" * len(names))
        rows = [
            ("Thời gian có nhãn (s)", "labeled_s"),
            ("Thời gian phòng trống (s)", "empty_s"),
            ("Số lần báo giả", "false_alarms"),
            ("Báo giả mỗi giờ phòng trống", "false_alarms_per_hour"),
            ("Tổng thời gian báo giả (s)", "false_alarm_s"),
            ("Tổng thời gian bỏ sót khi có người (s)", "missed_s"),
            ("Khoảng bỏ sót dài nhất (s)", "longest_miss_s"),
            ("Thời gian 'chưa chắc' (s)", "uncertain_s"),
            ("Thời gian chưa có quyết định (s)", "undecided_s"),
        ]
        for title, key in rows:
            out.append(f"| {title} | " + " | ".join(_fmt(tl[m].get(key)) for m in names) + " |")
        for title, key in (("Độ trễ vào phòng (s)", "entry_latency_s"), ("Độ trễ ra khỏi phòng (s)", "exit_latency_s")):
            cells = []
            for m in names:
                lat = tl[m][key]
                cells.append(f"trung vị {_fmt(lat['median'])}, p90 {_fmt(lat['p90'])}, lớn nhất {_fmt(lat['max'])}; "
                             f"{lat['detected']}/{lat['transitions']} lần trong giới hạn")
            out.append(f"| {title} | " + " | ".join(cells) + " |")
        out.append("")
    else:
        out.append("Không có dữ liệu test để phát lại.\n")

    out.append("## Theo cửa sổ (4 s)\n")
    for split, d in ev["windows"].items():
        for kind in ("int8", "float"):
            w = d[kind]
            if not w.get("windows"):
                continue
            out.append(f"### Tập {split}, mô hình {kind}: {w['windows']} cửa sổ, độ chính xác {_fmt(w['accuracy'])}\n")
            out.append(_cm_table(w["confusion"], C.CLASSES))
            out.append("| Lớp | precision | recall | F1 | số cửa sổ |\n|---|---|---|---|---|")
            for name, m in w["per_class"].items():
                out.append(f"| {name} | {_fmt(m['precision'])} | {_fmt(m['recall'])} | {_fmt(m['f1'])} | {m['support']} |")
            b = w["binary"]
            out.append(f"\nNhị phân (gộp lớp) — độ chính xác {_fmt(b['accuracy'])}:\n")
            out.append(_cm_table(b["confusion"], ["trống", "có người"]))
            for name, m in b["per_class"].items():
                out.append(f"- {name}: precision {_fmt(m['precision'])}, recall {_fmt(m['recall'])}")
            out.append("")

    out.append("## Trước và sau lượng tử hóa INT8\n")
    for split, q in ev["quantization"].items():
        out.append(f"- Tập {split}: {q['windows']} cửa sổ, argmax trùng {_fmt(q['argmax_agreement'])}, "
                   f"độ chính xác float {_fmt(q['accuracy_float'])} / INT8 {_fmt(q['accuracy_int8'])}, "
                   f"sai lệch điểm có người lớn nhất {_fmt(q['max_abs_occupied_score_diff'])}")
    out.append("")
    cal = ev["calibration"]
    out.append("## Điểm mô hình và xác suất\n")
    out.append(cal["note"] + "\n")
    if cal["table"]:
        out.append(f"- ECE của điểm thô trên test: {_fmt(cal['ece_raw_score_test'])}; sau hiệu chuẩn: "
                   f"{_fmt(cal['ece_calibrated_test'])} (bảng {len(cal['table']['x'])} điểm, {cal['table']['n']} cửa sổ validation)")
    else:
        out.append("- Không tạo bảng hiệu chuẩn (validation quá ít hoặc chỉ có một loại nhãn).")
    out.append("")

    d = r["data"]
    out.append("## Dữ liệu\n")
    out.append(f"Chia tập theo **{d['split_mode']}**; normalization học từ tập train.\n")
    out.append("| Tập | cửa sổ | theo lớp | bị loại |\n|---|---|---|---|")
    for n, w in d["windows"].items():
        out.append(f"| {n} | {w['count']} | {w['per_class']} | {w['rejected']} |")
    out.append("\n| Phiên | nguồn | tập | thời lượng (s) | tốc độ mẫu (Hz) | khung radar/s | thiếu ô | mất mẫu | có nhãn | cảnh báo |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for q in d["quality"]:
        sp = d["session_split"].get(q["session_id"], "")
        out.append(f"| {q['session_id']} | {q['source']} | {sp} | {q['duration_s']} | {q['sample_rate_hz']} | "
                   f"{_fmt(q['radar_rate_hz_est'], 1)} | {q['missing_slot_fraction']:.2%} | {q['seq_gaps']} | "
                   f"{q['labeled_fraction']:.0%} | {'; '.join(q['warnings']) or '—'} |")
    if d["split_warnings"]:
        out.append("\nCảnh báo chia tập: " + "; ".join(d["split_warnings"]))
    out.append("")
    t = r["training"]
    out.append(f"## Huấn luyện\n\nKiến trúc {r['architecture']['input']} → "
               + " → ".join(f"Dense {h} ReLU" for h in r["architecture"]["hidden"])
               + f" → Dense {r['architecture']['output']} Softmax. Epoch tốt nhất {t['best_epoch']} / "
               f"{t['epochs_run']} (early stopping theo val_loss). Class weight: {t['class_weight']}.\n")
    q = r["quantization"]
    out.append(f"Lượng tử hóa full INT8: input scale {q['input_scale']:.6g} zp {q['input_zero_point']}, output scale "
               f"{q['output_scale']:.6g} zp {q['output_zero_point']}, {q['representative_samples']} cửa sổ train làm dữ liệu "
               f"hiệu chuẩn; op: {', '.join(q['ops']) or '—'}.\n")
    if "package" in r:
        p = r["package"]
        out.append(f"Gói: {p['file']} ({p['size']} byte), SHA-256 `{p['sha256']}`, "
                   f"{'đã ký' if p['signed'] else 'CHƯA ký'}, {p['test_vectors']} vector kiểm tra.\n")
    return "\n".join(out) + "\n"
