"""Tạo / kiểm tra cặp khóa ký gói mô hình (ECDSA P-256).

    python tools/keys.py init     # tạo secrets/model_signing_key.pem + firmware/keys/model_signing_pub.pem
    python tools/keys.py show     # in khóa công khai và đường dẫn
    python tools/keys.py pub      # (re)tạo khóa công khai từ khóa riêng đã có

Khóa RIÊNG chỉ nằm trên máy chạy backend/huấn luyện (biến MODEL_SIGNING_KEY trong .env trỏ tới nó).
Khóa CÔNG KHAI được nhúng vào firmware khi build. Mất khóa riêng => phải build lại firmware với khóa mới.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))

from presence_ml.package import generate_keypair, public_from_private  # noqa: E402

PRIV = ROOT / "secrets" / "model_signing_key.pem"
PUB = ROOT / "firmware" / "keys" / "model_signing_pub.pem"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["init", "show", "pub"])
    ap.add_argument("--force", action="store_true", help="ghi đè khóa đã có (firmware cũ sẽ từ chối gói mới)")
    a = ap.parse_args()
    if a.cmd == "init":
        if PRIV.exists() and not a.force:
            print(f"Đã có {PRIV} (dùng --force để tạo khóa mới)")
            return 1
        priv, pub = generate_keypair()
        PRIV.parent.mkdir(parents=True, exist_ok=True)
        PRIV.write_bytes(priv)
        try:
            os.chmod(PRIV, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        PUB.parent.mkdir(parents=True, exist_ok=True)
        PUB.write_bytes(pub)
        print(f"Khóa riêng:     {PRIV}  (KHÔNG commit, KHÔNG gửi đi)")
        print(f"Khóa công khai: {PUB}  (nhúng vào firmware khi build)")
        print("Đặt MODEL_SIGNING_KEY=" + str(PRIV) + " trong file .env của backend.")
        return 0
    if a.cmd == "pub":
        if not PRIV.exists():
            print(f"Không có {PRIV}: chạy 'init' trước")
            return 1
        PUB.parent.mkdir(parents=True, exist_ok=True)
        PUB.write_bytes(public_from_private(PRIV.read_bytes()))
        print(f"Đã ghi {PUB}")
        return 0
    print(f"Khóa riêng: {PRIV} ({'có' if PRIV.exists() else 'CHƯA có'})")
    print(f"Khóa công khai: {PUB} ({'có' if PUB.exists() else 'CHƯA có'})")
    if PUB.exists():
        print(PUB.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
