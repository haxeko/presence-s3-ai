#!/bin/sh
# Kiểm tra ACL của Mosquitto (deployment/mosquitto/config/acl) bằng broker thật trong Docker.
# Dùng: sh deployment/scripts/test-mosquitto-acl.sh   (Linux, macOS, WSL hoặc Git Bash; cần Docker)
# Tạo cấu hình tạm với người dùng thử, chạy broker ở cổng 18831, kiểm tra quyền rồi dọn dẹp.
set -e
IMAGE=eclipse-mosquitto:2.0.22
PORT=18831
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
NAME="presence-acl-test-$$"
# Entrypoint của image (chạy bằng root) đổi chủ /mosquitto thành uid 1883, nên người dùng thường không xóa được thư mục
# tạm: xóa bằng một container.
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run --rm -v "$TMP:/t" "$IMAGE" rm -rf /t/config >/dev/null 2>&1 || true
  rm -rf "$TMP" || true
}
trap cleanup EXIT

mkdir -p "$TMP/config"
cp "$HERE/mosquitto/config/acl" "$TMP/config/acl"
cat > "$TMP/config/mosquitto.conf" <<CONF
listener $PORT
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
CONF
touch "$TMP/config/passwd"
# Quyền file (tránh cảnh báo của mosquitto 2.0); đặt trước khi container đổi chủ thư mục
chmod 0700 "$TMP/config" && chmod 0600 "$TMP/config/"*
for u in "presence-backend backpw" "s3-aaaaaa devpw"; do
  set -- $u
  docker run --rm -v "$TMP/config:/mosquitto/config" "$IMAGE" mosquitto_passwd -b /mosquitto/config/passwd "$1" "$2" >/dev/null 2>&1
done
docker run -d --name "$NAME" --network host -v "$TMP/config:/mosquitto/config" "$IMAGE" \
  mosquitto -c /mosquitto/config/mosquitto.conf >/dev/null
sleep 1

cat > "$TMP/checks.sh" <<'CHECKS'
P=presence/v1/dev
B="-h 127.0.0.1 -p $PORT -u presence-backend -P backpw"
A="-h 127.0.0.1 -p $PORT -u s3-aaaaaa -P devpw"
FAIL=0
result() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (got '$2', want '$3')"; FAIL=1; fi; }

mosquitto_sub $B -t "$P/#" -v -W 4 > /tmp/backend.txt 2>/dev/null &
sleep 0.5
mosquitto_pub $A -t "$P/s3-aaaaaa/samples" -m ok-samples -q 1
mosquitto_pub $A -t "$P/s3-aaaaaa/desired/model" -m forged-model -q 1
mosquitto_pub $A -t "$P/s3-bbbbbb/samples" -m forged-other -q 1
mosquitto_pub $A -t "$P/s3-aaaaaa/cmd" -m forged-cmd -q 1
wait
result "thiết bị ghi samples của mình" "$(grep -c ok-samples /tmp/backend.txt)" 1
result "thiết bị không tự đặt desired/model" "$(grep -c forged-model /tmp/backend.txt)" 0
result "thiết bị không ghi vào thiết bị khác" "$(grep -c forged-other /tmp/backend.txt)" 0
result "thiết bị không tự gửi cmd" "$(grep -c forged-cmd /tmp/backend.txt)" 0

mosquitto_pub $B -t "$P/s3-aaaaaa/desired/model" -m '{"model_id":"x"}' -r -q 1
mosquitto_pub $B -t "$P/s3-bbbbbb/desired/model" -m '{"model_id":"other"}' -r -q 1
mosquitto_sub $A -t "$P/s3-aaaaaa/desired/model" -C 1 -W 3 > /tmp/own.txt 2>/dev/null || true
result "thiết bị đọc desired/model của mình (retained)" "$(grep -c '"x"' /tmp/own.txt)" 1
mosquitto_sub $A -t "$P/s3-bbbbbb/#" -t "$P/+/samples" -W 3 > /tmp/other.txt 2>/dev/null || true
result "thiết bị không đọc được thiết bị khác" "$(wc -c < /tmp/other.txt | tr -d ' ')" 0

mosquitto_sub $B -t "$P/s3-aaaaaa/status" -v -W 5 > /tmp/lwt.txt 2>/dev/null &
sleep 0.5
mosquitto_sub $A -t "$P/s3-aaaaaa/cmd" --will-topic "$P/s3-aaaaaa/status" \
  --will-payload '{"online":false,"reason":"lwt"}' --will-retain --will-qos 1 -k 5 &
SUB=$!
sleep 1
kill -9 $SUB
wait
result "LWT khi mất kết nối đột ngột" "$(grep -c '"lwt"' /tmp/lwt.txt)" 1

if mosquitto_pub -h 127.0.0.1 -p $PORT -u s3-aaaaaa -P wrong -t "$P/s3-aaaaaa/samples" -m x 2>/dev/null; then
  result "sai mật khẩu bị từ chối" no yes
else
  result "sai mật khẩu bị từ chối" yes yes
fi
exit $FAIL
CHECKS
docker run --rm --network host -e PORT=$PORT -v "$TMP/checks.sh:/checks.sh:ro" "$IMAGE" sh /checks.sh
