"""Thu luồng byte thô của LD2410 qua bộ chuyển USB-UART (để phát lại bằng tools/ld2410_replay.py hoặc
tools/fake_device.py --capture). Bật Engineering Mode trước khi thu (--engineering).

    python tools/ld2410_capture.py --serial COM6 --out phong-khach.ld2410.log --seconds 600 --engineering

Định dạng file: mỗi dòng "<ms từ lúc bắt đầu> <hex>", dòng bắt đầu bằng # là chú thích. Cuối phiên in thống kê
(khung hợp lệ/giây, khung lỗi) do bộ phân tích LD2410 của dự án tính.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training"))

from presence_ml import ld2410  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--baud", type=int, default=256000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--engineering", action="store_true", help="gửi lệnh bật Engineering Mode trước khi thu")
    a = ap.parse_args()
    import serial  # pyserial

    ser = serial.Serial(a.serial, a.baud, timeout=0.05)
    parser = ld2410.Parser()
    if a.engineering:
        for cmd, val in ((ld2410.CMD_ENABLE_CONFIG, b"\x01\x00"), (ld2410.CMD_ENGINEERING_ON, b""),
                         (ld2410.CMD_END_CONFIG, b"")):
            ser.write(ld2410.build_command(cmd, val))
            time.sleep(0.2)
    start = time.monotonic()
    n = 0
    with open(a.out, "w", encoding="ascii") as f:
        f.write(f"# LD2410 capture {time.strftime('%Y-%m-%d %H:%M:%S')} {a.serial} {a.baud} baud\n")
        while time.monotonic() - start < a.seconds:
            data = ser.read(512)
            if not data:
                continue
            t = (time.monotonic() - start) * 1000
            f.write(f"{t:.1f} {data.hex()}\n")
            for ev in parser.feed(data):
                n += isinstance(ev, ld2410.Report) and ev.full_layout
    s = parser.stats
    dur = time.monotonic() - start
    print(f"{dur:.0f} s: {s.bytes} byte, {s.reports} khung báo cáo ({n} Engineering đủ 9+9 gate, "
          f"{n / dur:.1f}/s), lỗi footer {s.bad_footer}, lỗi nội dung {s.bad_payload}, độ dài sai {s.bad_length}")


if __name__ == "__main__":
    main()
