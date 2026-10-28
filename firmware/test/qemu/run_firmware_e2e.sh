#!/bin/bash
# Runs the REAL firmware in Espressif's QEMU (ESP32-S3) with OpenCores Ethernet instead of Wi-Fi and UART1 (radar)
# on a TCP socket, so the whole data/model loop can be tested without hardware:
#   tools/ld2410_replay.py --tcp localhost:5556  ->  UART1 -> firmware -> MQTT/HTTP at 10.0.2.2 (= this host)
# Run inside espressif/idf:v5.5.6 with --network host, from the repository root. Needs Mosquitto on :1883 and the
# backend on :8000 of the host. Extra sdkconfig lines (credentials) in $QEMU_SDKCONFIG_EXTRA (file path).
# QEMU_CONSOLE_PORT=5555 puts the console/log UART0 on a TCP socket instead of stdio (to type console commands such
# as `mem`); QEMU then waits for that connection before starting.
# QEMU_WEB_PORT=8080 forwards that host port to the device's configuration web page (port 80).
set -euo pipefail
cd "$(dirname "$0")/../.."
DEFAULTS="sdkconfig.defaults;sdkconfig.qemu"
if [ -n "${QEMU_SDKCONFIG_EXTRA:-}" ]; then DEFAULTS="$DEFAULTS;$QEMU_SDKCONFIG_EXTRA"; fi
rm -f build-qemu/sdkconfig
idf.py -B build-qemu -D SDKCONFIG=build-qemu/sdkconfig -D SDKCONFIG_DEFAULTS="$DEFAULTS" build
cd build-qemu
python -m esptool --chip=esp32s3 merge_bin --output=qemu_flash.bin --fill-flash-size=4MB @flash_args >/dev/null
python - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["IDF_PATH"], "tools"))
from idf_py_actions.qemu_ext import QEMU_TARGETS
open("qemu_efuse.bin", "wb").write(QEMU_TARGETS["esp32s3"].default_efuse)
PY
NIC="user,model=open_eth"
if [ -n "${QEMU_WEB_PORT:-}" ]; then NIC="${NIC},hostfwd=tcp::${QEMU_WEB_PORT}-:80"; fi
UART0="mon:stdio"
if [ -n "${QEMU_CONSOLE_PORT:-}" ]; then UART0="tcp::${QEMU_CONSOLE_PORT},server"; fi
exec qemu-system-xtensa -M esp32s3 -m 32M -nographic \
    -drive file=qemu_flash.bin,if=mtd,format=raw \
    -drive file=qemu_efuse.bin,if=none,format=raw,id=efuse \
    -global driver=nvram.esp32s3.efuse,property=drive,value=efuse \
    -global driver=timer.esp32s3.timg,property=wdt_disable,value=true \
    -nic "${NIC}" \
    -serial "${UART0}" -serial tcp::5556,server,nowait
