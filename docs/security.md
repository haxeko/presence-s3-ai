# Bảo mật: token, TLS, khóa ký, khi đưa ra Internet

## Hai chế độ triển khai

| | LAN tin cậy (mặc định) | Có cổng mở ra Internet |
|---|---|---|
| MQTT | `mqtt://` cổng 1883, có mật khẩu và ACL | **chỉ** `mqtts://` cổng 8883 |
| Backend / dashboard | `http://` cổng 8000, có `ADMIN_TOKEN` | **chỉ** `https://` (reverse proxy hoặc uvicorn có TLS), không mở 8000 |
| Tải mô hình | `http://…/api/device/…` + token thiết bị | `https://…` + token thiết bị |
| Gói mô hình | luôn ký ECDSA P-256, firmware kiểm tra chữ ký | như LAN |

Kể cả trong LAN: không có kết nối MQTT ẩn danh, API quản trị cần token (trừ khi bạn chủ động để trống `ADMIN_TOKEN` khi thử trên máy), và firmware chỉ chạy mô hình có chữ ký hợp lệ.

## Bí mật nằm ở đâu (không có trong repo)

| Bí mật | Nơi lưu | Ghi chú |
|---|---|---|
| Mật khẩu MQTT của backend, `ADMIN_TOKEN`, `DEVICE_TOKENS` | `.env` ở gốc repo | `.gitignore`; mẫu: `deployment/.env.example` |
| Khóa riêng ký mô hình | `secrets/model_signing_key.pem` | Tạo bằng `python tools/keys.py init`; chỉ để trên máy chạy backend/huấn luyện |
| Mật khẩu người dùng MQTT | `deployment/mosquitto/config/passwd` | Mosquitto lưu dạng băm (PBKDF2-SHA512) |
| Khóa riêng TLS của máy chủ | `deployment/mosquitto/config/certs/`, thư mục của reverse proxy | `.gitignore` |
| Wi-Fi, mật khẩu MQTT, token của **thiết bị** | NVS của ESP32 (lệnh `config set` trên console), hoặc `firmware/sdkconfig.secrets` (biên dịch vào firmware) | `sdkconfig.secrets` có trong `.gitignore` |

Repo chỉ chứa:
- file mẫu (`.env.example`, `sdkconfig.secrets.example`);
- **khóa công khai thử nghiệm** và các gói mô hình mẫu ký bằng khóa thử nghiệm. Khóa riêng thử nghiệm không được commit. Firmware thật phải được build với khóa công khai của bạn (`firmware/keys/model_signing_pub.pem`, file này cũng nằm ngoài git).

Trước khi commit, kiểm tra: `git status --ignored` và `git add -n .`. Không được có `.pem` khóa riêng, `.env`, `passwd`, `sdkconfig.secrets`.

**Hạn chế đã biết:** NVS và firmware **không mã hóa**: ai cầm được bo mạch có thể đọc flash và lấy mật khẩu Wi-Fi/MQTT và token thiết bị. Dự án chưa bật và chưa thử Flash Encryption, NVS Encryption và Secure Boot của ESP32-S3. Khi bo đặt ở nơi người lạ tiếp cận được, hãy bật chúng (xem tài liệu ESP-IDF "Security") và dùng mật khẩu MQTT riêng cho từng thiết bị, để có thể thu hồi từng cái.

## Chuỗi tin cậy của mô hình

1. Backend ký SHA-256 của toàn bộ gói bằng khóa riêng ECDSA P-256. Khóa công khai được **nhúng lúc build firmware**. Nếu `CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE=y` (mặc định) mà thiếu file khóa thì build dừng.
2. Thông báo `desired/model` chỉ mang mã, phiên bản, kích thước và SHA-256. Thiết bị tự ghép URL từ `server_url` đã cấu hình, nên MQTT **không** chuyển hướng được thiết bị sang máy chủ khác. `model_id` chỉ nhận `[A-Za-z0-9._-]`.
3. Trước khi kích hoạt, thiết bị kiểm tra:
   - SHA-256 toàn gói và từng section;
   - chữ ký;
   - `fw_api`;
   - hợp đồng đầu vào;
   - bộ nhớ;
   - flatbuffer;
   - chạy thử các vector kiểm tra.

   Gói tối đa 192 KB (`CONFIG_PRESENCE_MODEL_MAX_PACKAGE_KB`).
4. Chỉ backend được ghi `desired/model` (ACL MQTT). Chỉ mô hình **đã duyệt** mới tải được.
5. Thiết bị **không** bắt phiên bản phải tăng dần, để cho phép khôi phục bản cũ. Vì vậy, người có quyền của backend có thể cài lại một mô hình cũ, nhưng chỉ mô hình có chữ ký hợp lệ.

## Xác thực

- **API quản trị**: `Authorization: Bearer <ADMIN_TOKEN>` (hoặc `X-Admin-Token`), so sánh trong thời gian hằng. Không đặt `ADMIN_TOKEN` thì API mở và backend ghi cảnh báo lúc khởi động.
  - Token quản trị có toàn quyền: phát hành mô hình, xóa dữ liệu, chạy huấn luyện, nhập bộ dữ liệu từ đường dẫn trên máy chủ.
  - Dashboard lưu token trong `localStorage` của trình duyệt. Mọi chuỗi đến từ thiết bị hoặc người dùng đều được escape trước khi chèn vào trang.
- **Tải gói**: `Authorization: Bearer <token thiết bị>` (một trong `DEVICE_TOKENS`). Token không gắn với một thiết bị cụ thể: mọi token hợp lệ tải được mọi mô hình đã duyệt. Gói mô hình không chứa dữ liệu thô.
- **MQTT**: mỗi thiết bị có người dùng riêng (tên = mã thiết bị). ACL ([mqtt-topics.md](mqtt-topics.md#quyền-trên-broker-acl)) giới hạn thiết bị trong chủ đề của chính nó. Đã kiểm tra bằng broker thật: `sh deployment/scripts/test-mosquitto-acl.sh`.
- Không cần token: `/api/health`, `/docs`, `/openapi.json` (chỉ mô tả API, không có dữ liệu). Muốn ẩn thì chặn ở reverse proxy.
- Chưa có giới hạn số lần thử (rate limit). Khi mở ra Internet, đặt backend sau reverse proxy có giới hạn, và dùng token dài ngẫu nhiên:

  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## TLS

Thiết bị kiểm tra chứng chỉ máy chủ theo một trong hai cách:
- **Bộ chứng chỉ gốc công khai của ESP-IDF** (mặc định): dùng khi máy chủ có chứng chỉ Let's Encrypt hoặc CA thương mại cho một **tên miền**. Đây là cách nên dùng khi đưa ra Internet.
- **CA riêng**: đặt chứng chỉ CA vào `firmware/certs/ca.pem`, chạy `idf.py reconfigure build`. Firmware chỉ tin CA đó; trạng thái báo `tls_trust: "private_ca"`. Dùng trong LAN với CA tự tạo.

Không có chế độ "bỏ qua kiểm tra chứng chỉ". Thiết bị kiểm tra tên/IP trong SAN, nhưng **không kiểm tra hạn** chứng chỉ (mbedTLS của ESP-IDF mặc định không bật `MBEDTLS_HAVE_TIME_DATE`) và không kiểm tra thu hồi. Hãy thay chứng chỉ trước khi hết hạn như bình thường.

### Tạo CA riêng và chứng chỉ máy chủ (LAN)

Dùng OpenSSL (có sẵn trong Git for Windows hoặc WSL). Thay `192.168.1.10` bằng IP máy chạy Mosquitto/backend:

```bash
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 3650 -subj "/CN=Presence CA" -out ca.crt
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=presence-server" -out server.csr
printf "subjectAltName=IP:192.168.1.10,DNS:localhost\nextendedKeyUsage=serverAuth\n" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -sha256 -extfile server.ext -out server.crt
```

- `ca.key` là thứ quan trọng nhất: cất riêng, không để trên máy chủ.
- Chép `ca.crt`, `server.crt`, `server.key` vào `deployment/mosquitto/config/certs/`, rồi bỏ chú thích khối `listener 8883` trong `mosquitto.conf`. Khi không còn thiết bị nào dùng `listener 1883`, xóa nó đi.
- Backend:
  - kết nối broker qua TLS: `MQTT_PORT=8883`, `MQTT_TLS=1`, `MQTT_CA_FILE=<đường dẫn ca.crt>`. `MQTT_HOST` phải khớp SAN, ví dụ `localhost`;
  - phục vụ HTTPS bằng `uvicorn … --ssl-keyfile server.key --ssl-certfile server.crt`, hoặc đặt sau reverse proxy (Caddy, nginx).
- Firmware: chép `ca.crt` thành `firmware/certs/ca.pem`; `mqtt_uri = mqtts://192.168.1.10:8883`, `server_url = https://192.168.1.10:8443` (hoặc cổng của proxy).

## Danh sách kiểm tra trước khi mở ra Internet

- [ ] Chỉ mở 8883 (MQTT TLS) và 443 (HTTPS). Đóng 1883 và 8000 trên tường lửa/router.
- [ ] `ADMIN_TOKEN` và `DEVICE_TOKENS` dài, ngẫu nhiên. Mật khẩu MQTT riêng cho từng thiết bị.
- [ ] Chứng chỉ có tên miền (Let's Encrypt) hoặc CA riêng đã nhúng vào firmware.
- [ ] `CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE=y`; khóa riêng ký mô hình chỉ có trên máy backend và có bản sao lưu.
- [ ] Sao lưu `data/` (dữ liệu, nhãn, mô hình) và `secrets/`.
- [ ] Cân nhắc Flash Encryption / NVS Encryption / Secure Boot nếu người lạ tiếp cận được bo.
- [ ] Image Docker và thư viện vẫn ở phiên bản đã cố định. Chỉ nâng cấp có chủ đích, sau khi chạy lại test ([verification.md](verification.md)).

## Quyền file của Mosquitto

- **Windows (Docker Desktop):** Mosquitto 2.0.22 cảnh báo khi `passwd`/`acl` có quyền đọc cho mọi người. Thư mục mount từ ổ Windows luôn hiện như vậy. Bản 2.0.22 vẫn chạy và chỉ cảnh báo; bản sau có thể từ chối. Khi đó, đặt cấu hình trong WSL hoặc trong một named volume.
- **Linux:** entrypoint của image `eclipse-mosquitto` (chạy bằng root) đổi chủ thư mục `deployment/mosquitto/config` sang uid 1883 mỗi lần khởi động. Mosquitto đọc `passwd`/`acl` **sau khi** hạ quyền xuống uid này, nên không thể mount chỉ đọc một file 0600 thuộc người dùng khác (đã thử). Hệ quả: muốn sửa `acl` hoặc `git pull` thay đổi file trong thư mục đó, trước hết chạy `sudo chown -R "$USER" deployment/mosquitto/config`, rồi khởi động lại broker để nó đổi chủ lại. Tạo người dùng MQTT vẫn dùng `deployment/scripts/mosquitto-user.sh` như bình thường.
