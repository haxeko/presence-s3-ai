# Cài đặt và chạy trên Windows (PowerShell)

Cần có:
- Windows 10/11 64-bit.
- **Python 3.12** từ python.org; khi cài, chọn "Add python.exe to PATH" và "py launcher".
- **Docker Desktop**, cho Mosquitto. Tùy chọn: chạy backend trong Docker hoặc build firmware bằng Docker.
- **ESP-IDF v5.5.6**, để build và nạp firmware.
- Git.

## 1. Backend, huấn luyện, dashboard

```powershell
git clone https://github.com/haxeko/presence-s3-ai
cd presence-s3-ai
powershell -ExecutionPolicy Bypass -File deployment\scripts\setup-windows.ps1
```

Script trên lần lượt:
1. tạo `.venv` và cài `requirements\dev.txt`, gồm TensorFlow 2.21.0 CPU (khoảng 600 MB tải về);
2. cài gói của dự án ở chế độ editable;
3. tạo `.env` từ `deployment\.env.example`;
4. tạo cặp khóa ký mô hình (`python tools\keys.py init`).

Mở `.env` để đổi `MQTT_PASSWORD`, `DEVICE_TOKENS`, `ADMIN_TOKEN`. `MODEL_SIGNING_KEY` trỏ tới `secrets\model_signing_key.pem` (khóa riêng, **không chia sẻ, không commit**).

## 2. Mosquitto (Docker)

```powershell
.\deployment\scripts\mosquitto-user.ps1 presence-backend     # nhập đúng MQTT_PASSWORD của .env
.\deployment\scripts\mosquitto-user.ps1 s3-a1b2c3            # mỗi thiết bị: tên = mã thiết bị (in trong log khi khởi động)
docker compose -f deployment\docker-compose.yml up -d
docker compose -f deployment\docker-compose.yml logs mosquitto
```

Tên đăng nhập MQTT của thiết bị **phải** là mã thiết bị: ACL (`deployment\mosquitto\config\acl`) chỉ cho thiết bị ghi dữ liệu của chính nó và đọc cấu hình/lệnh của chính nó. Firmware tự dùng mã thiết bị khi để trống `mqtt_user`. Khi tạo hoặc đổi mật khẩu người dùng, khởi động lại broker.

> Mosquitto 2.0.22 báo cảnh báo quyền file `passwd` trên ổ Windows (bind mount). Hiện chỉ là cảnh báo; các bản Mosquitto sau có thể từ chối. Khi đó hãy dùng named volume hoặc WSL. Xem [security.md](security.md).

## 3. Chạy backend và mở dashboard

```powershell
.\.venv\Scripts\python.exe -m uvicorn presence_server.main:app --host 0.0.0.0 --port 8000
```

Mở <http://localhost:8000/dashboard/>, vào tab **Cài đặt**, nhập `ADMIN_TOKEN`. API docs (Swagger) ở <http://localhost:8000/docs>. Mở cổng 8000 và 1883 trên Windows Firewall (mạng Private) để ESP32 trong LAN kết nối được.

Muốn chạy backend trong Docker (image có TensorFlow, khoảng 2,5 GB):

```powershell
# trong .env: MODEL_SIGNING_KEY=/secrets/model_signing_key.pem
docker compose -f deployment\docker-compose.yml --profile backend up -d --build
```

## 4. Firmware ESP32-S3

### Cài ESP-IDF v5.5.6

Cài bằng **ESP-IDF Installation Manager** (EIM) của Espressif, chọn phiên bản **v5.5.6**. Phiên bản này đã được kiểm chứng; các bản khác, kể cả v6.x, chưa thử, và [`idf_component.yml`](../firmware/components/presence/idf_component.yml) giới hạn ở `>=5.5,<5.6`. Mở "ESP-IDF 5.5 PowerShell" từ menu Start.

### Cấu hình bí mật (không commit)

```powershell
cd firmware
Copy-Item sdkconfig.secrets.example sdkconfig.secrets
notepad sdkconfig.secrets      # Wi-Fi, MQTT URI (IP máy chạy Mosquitto), mật khẩu MQTT, URL backend, token thiết bị
```

Cũng có thể để trống và cấu hình sau khi nạp: qua **trang web trên thiết bị** `http://<IP thiết bị>/` (khi thiết bị đã vào Wi-Fi, xem [home-assistant.md](home-assistant.md)), hoặc qua console nối tiếp:

```
presence> config set wifi_ssid TenWifi
presence> config set wifi_pass MatKhau
presence> config set mqtt_uri mqtt://192.168.1.10:1883
presence> config set mqtt_pass MatKhauMQTT
presence> config set server_url http://192.168.1.10:8000
presence> config set device_token TokenThietBi
presence> reboot
```

Bỏ trống `mqtt_user` thì firmware dùng mã thiết bị làm tên đăng nhập.

### Build và nạp

Cần khóa công khai ở `firmware\keys\model_signing_pub.pem` (`python tools\keys.py init` đã tạo ở bước 1).

```powershell
cd firmware
idf.py set-target esp32s3
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.secrets" build
idf.py -p COM5 flash monitor        # cổng "UART" của DevKitC-1; thoát monitor: Ctrl+]
```

Lần đầu build, component manager tải `esp-tflite-micro` 1.4.1 và `esp-nn` 1.4.1 theo `dependencies.lock`.

Build bằng Docker (không cần cài ESP-IDF), nạp bằng esptool:

```powershell
docker run --rm -v ${PWD}:/project -w /project/firmware espressif/idf:v5.5.6 idf.py build
.\.venv\Scripts\python.exe -m pip install esptool
cd firmware\build
..\..\.venv\Scripts\python.exe -m esptool --chip esp32s3 -p COM5 -b 460800 write_flash "@flash_args"
```

Trong lần chạy đầu, log phải có các dòng sau:

```
I (...) app: Presence S3 AI 0.1.0, device s3-xxxxxx, boot 1
I (...) radar: radar V2.xx..., protocol 1, gate 75 cm, engineering on, config closed
I (...) net: MQTT connected
```

Gõ `status` trong console để xem tóm tắt, và `mem` để xem heap, stack từng task và thời gian một lần quyết định ([hardware-measurements.md](hardware-measurements.md)). Thiết bị xuất hiện ở tab **Tổng quan** của dashboard.

Dùng TLS (`mqtts://`, `https://`) với CA tự tạo: chép chứng chỉ CA vào `firmware\certs\ca.pem` trước khi build ([security.md](security.md#tls)).

## 5. Thu dữ liệu, huấn luyện, phát hành

1. **Thu dữ liệu** (tab Thu dữ liệu): [data-collection.md](data-collection.md).
2. **Học phòng trống** (tùy chọn, để đối chứng): tab Học phòng trống.
3. **Huấn luyện** (tab Huấn luyện): xem tiến trình và log, kết quả nằm ở tab Mô hình. Chạy bằng dòng lệnh: [training.md](training.md).
4. **Đọc báo cáo**, so sánh với mô hình đang chạy và với phương pháp ngưỡng, rồi **Duyệt**.
5. **Phát hành** xuống thiết bị (tab Mô hình). Thiết bị tải về, kiểm tra, chạy thử rồi kích hoạt; bảng "Lịch sử phát hành" hiện `active` hoặc `failed` kèm lý do. Nút **Khôi phục bản trước** chuyển về mô hình cũ mà không cần tải lại.

## 6. Kiểm thử trên Windows

```powershell
.\.venv\Scripts\python.exe -m pytest                       # huấn luyện + backend
docker run --rm -v ${PWD}:/project -w /project espressif/idf:v5.5.6 firmware/test/qemu/run_target_test.sh
```

Test C++ của phần lõi firmware chạy trên máy tính (CMake + g++ + OpenSSL, hoặc mbedTLS của ESP-IDF). Trên Windows dùng chính image ESP-IDF:

```powershell
docker run --rm -v ${PWD}:/project -w /project espressif/idf:v5.5.6 bash -c "cmake -S firmware/test/host -B /tmp/b -G Ninja && cmake --build /tmp/b && /tmp/b/host_tests"
```
