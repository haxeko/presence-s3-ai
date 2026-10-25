#!/bin/sh
# Tạo/đổi mật khẩu người dùng MQTT (Linux/macOS/WSL). Dùng: deployment/scripts/mosquitto-user.sh <user> <password>
set -e
CFG="$(cd "$(dirname "$0")/../mosquitto/config" && pwd)"
[ -f "$CFG/passwd" ] || touch "$CFG/passwd"
docker run --rm -v "$CFG:/mosquitto/config" eclipse-mosquitto:2.0.22 mosquitto_passwd -b /mosquitto/config/passwd "$1" "$2"
