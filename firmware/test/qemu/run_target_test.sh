#!/bin/bash
# Builds firmware/test/target for the ESP32-S3 and runs it in Espressif's QEMU (no board needed).
# Run inside the ESP-IDF container, from the repository root:
#   docker run --rm -v "$PWD":/project -w /project espressif/idf:v5.5.6 firmware/test/qemu/run_target_test.sh
# QEMU checks the code on the Xtensa ISA (float rounding, esp-nn kernels, mbedTLS, partition API); its timings are
# NOT those of a real ESP32-S3.
set -euo pipefail
cd "$(dirname "$0")/../target"
idf.py build
cd build
python -m esptool --chip=esp32s3 merge_bin --output=qemu_flash.bin --fill-flash-size=4MB @flash_args >/dev/null
python - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["IDF_PATH"], "tools"))
from idf_py_actions.qemu_ext import QEMU_TARGETS
open("qemu_efuse.bin", "wb").write(QEMU_TARGETS["esp32s3"].default_efuse)
PY
LOG=qemu_target_test.log
timeout 300 qemu-system-xtensa -M esp32s3 -m 32M -nographic \
    -drive file=qemu_flash.bin,if=mtd,format=raw \
    -drive file=qemu_efuse.bin,if=none,format=raw,id=efuse \
    -global driver=nvram.esp32s3.efuse,property=drive,value=efuse \
    -global driver=timer.esp32s3.timg,property=wdt_disable,value=true \
    -serial mon:stdio > "$LOG" 2>&1 &
QPID=$!
for _ in $(seq 1 280); do
    if grep -q "PRESENCE TARGET TEST \(PASS\|FAIL\)" "$LOG"; then break; fi
    if ! kill -0 $QPID 2>/dev/null; then break; fi
    sleep 1
done
kill $QPID 2>/dev/null || true
sed -n '/PRESENCE TARGET TEST START/,/PRESENCE TARGET TEST \(PASS\|FAIL\)/p' "$LOG"
grep -q "PRESENCE TARGET TEST PASS" "$LOG"
