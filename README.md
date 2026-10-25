# Presence S3 AI — cảm biến hiện diện ESP32-S3 + LD2410 với TinyML

Cảm biến hiện diện dùng **ESP32-S3** và radar 24 GHz **HLK-LD2410 / LD2410B / LD2410C**. Dự án gồm:

- **Firmware** (ESP-IDF): đọc năng lượng 9 gate chuyển động + 9 gate tĩnh ở Engineering Mode, gom thành mẫu 5 Hz. Phát hiện tại chỗ bằng mô hình INT8 (TensorFlow Lite Micro), hoặc bằng phương pháp ngưỡng khi chưa có mô hình. Logic 3 trạng thái: có người, trống, chưa chắc. Gửi mẫu về máy chủ qua MQTT.
- **Backend** (FastAPI + SQLite + Mosquitto): quản lý thiết bị, phiên thu, nhãn theo khoảng thời gian, học phòng trống, chạy tác vụ huấn luyện nền, phát hành và khôi phục mô hình.
- **Huấn luyện** (TensorFlow/Keras, chạy trên CPU): kiểm tra dữ liệu, chia tập theo phiên/ngày, tạo 72 đặc trưng, MLP 72-32-16-4, lượng tử hóa full INT8. Đánh giá bằng cách phát lại chuỗi thời gian qua cả mô hình và logic giữ trạng thái, so với phương pháp ngưỡng. Xuất gói mô hình có chữ ký.
- **Cập nhật mô hình qua mạng**: tách biệt với OTA firmware. Mô hình nằm ở hai vùng A/B, metadata an toàn khi mất điện. Thiết bị kiểm SHA-256, chữ ký ECDSA, tính tương thích, bộ nhớ và tự chạy thử trước khi kích hoạt; bản trước được giữ để khôi phục.
- **Dashboard tiếng Việt** và **công cụ chạy khi chưa có phần cứng**: thiết bị giả lập, phát lại/thu luồng byte LD2410, chạy firmware thật trong QEMU.

> **Trạng thái kiểm chứng.** Mọi phần đã được build và test trên máy tính, firmware đã chạy trong QEMU ESP32-S3. **Chưa chạy trên bo ESP32-S3 và radar thật.** Mô hình trong repo chỉ huấn luyện bằng **dữ liệu mô phỏng** để kiểm tra pipeline, không nói lên khả năng phát hiện người thật. Chi tiết: [docs/verification.md](docs/verification.md).

## Cấu trúc

| Thư mục | Nội dung |
|---|---|
| [`firmware/`](firmware) | ESP-IDF v5.5.x cho ESP32-S3. [`components/presence`](firmware/components/presence) là phần lõi C++ dùng chung (parser LD2410, đặc trưng, logic, gói mô hình, A/B) và phần riêng ESP32 (TFLM, mbedTLS, phân vùng). [`main/`](firmware/main) chứa ứng dụng (radar, quyết định, mạng, cập nhật, console). [`test/`](firmware/test) gồm test C++ trên máy tính, fixture và test trên QEMU |
| [`training/`](training) | Gói Python `presence_ml`: hợp đồng đặc trưng, LD2410, dữ liệu, cửa sổ, chia tập, huấn luyện, INT8, đánh giá, gói mô hình, bộ mô phỏng |
| [`backend/`](backend) | Gói Python `presence_server`: FastAPI, cầu nối MQTT, SQLite, tác vụ huấn luyện |
| [`dashboard/`](dashboard) | Giao diện web tĩnh tiếng Việt (HTML/JS, không thư viện ngoài), backend phục vụ ở `/dashboard/` |
| [`tools/`](tools) | `fake_device.py`, `ld2410_replay.py`, `ld2410_capture.py`, `keys.py`, `e2e_minimal_flow.py` |
| [`deployment/`](deployment) | Docker Compose (Mosquitto, backend), cấu hình Mosquitto (mật khẩu + ACL), `.env.example`, script PowerShell |
| [`docs/`](docs) | Tài liệu |
| [`requirements/`](requirements) | Phiên bản Python đã cố định sau kiểm chứng |

## Bắt đầu nhanh (Windows, PowerShell)

```powershell
git clone https://github.com/haxeko/presence-s3-ai; cd presence-s3-ai
powershell -ExecutionPolicy Bypass -File deployment\scripts\setup-windows.ps1   # venv Python 3.12, thư viện, .env, khóa ký
notepad .env                                                                    # đổi mật khẩu/token
.\deployment\scripts\mosquitto-user.ps1 presence-backend                        # cùng mật khẩu MQTT_PASSWORD trong .env
docker compose -f deployment\docker-compose.yml up -d                           # Mosquitto
.\.venv\Scripts\python.exe -m uvicorn presence_server.main:app --host 0.0.0.0 --port 8000
# Mở http://localhost:8000/dashboard/  (tab Cài đặt: nhập ADMIN_TOKEN)
```

Chưa có phần cứng thì chạy thiết bị giả lập, rồi chạy luồng tối thiểu (thu → huấn luyện → phát hành → thiết bị kích hoạt mô hình):

```powershell
.\deployment\scripts\mosquitto-user.ps1 s3-sim001
.\.venv\Scripts\python.exe tools\fake_device.py --device s3-sim001 --mqtt-user s3-sim001 --mqtt-pass <mật-khẩu> --device-token <DEVICE_TOKENS>
.\.venv\Scripts\python.exe tools\e2e_minimal_flow.py --admin-token <ADMIN_TOKEN> --device s3-sim001
```

Có phần cứng: [docs/wiring.md](docs/wiring.md) (đấu nối), [docs/windows-setup.md](docs/windows-setup.md) (build, nạp firmware, cấu hình Wi-Fi/MQTT qua console).

## Tài liệu

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Kiến trúc, các task trên ESP32, luồng dữ liệu, các giả định |
| [docs/wiring.md](docs/wiring.md) | Đấu nối ESP32-S3-DevKitC-1 với LD2410/B/C |
| [docs/windows-setup.md](docs/windows-setup.md) | Cài đặt và chạy trên Windows: backend, Mosquitto, ESP-IDF, nạp firmware |
| [docs/data-collection.md](docs/data-collection.md) | Thu dữ liệu, gán nhãn, tình huống khó, học phòng trống |
| [docs/training.md](docs/training.md) | Pipeline huấn luyện, cấu hình, đọc báo cáo đánh giá |
| [docs/model-update.md](docs/model-update.md) | Gói mô hình, A/B, phát hành, khôi phục, khóa ký |
| [docs/data-format.md](docs/data-format.md) | Hợp đồng 72 đặc trưng, định dạng mẫu, CSV, dataset.json |
| [docs/mqtt-topics.md](docs/mqtt-topics.md) | Chủ đề MQTT và nội dung |
| [docs/api.md](docs/api.md) | API HTTP của backend |
| [docs/ld2410.md](docs/ld2410.md) | Giao thức LD2410, biến thể hỗ trợ |
| [docs/security.md](docs/security.md) | Token, TLS, khóa ký, khi đưa ra Internet |
| [docs/hardware-measurements.md](docs/hardware-measurements.md) | Đo RAM, flash, thời gian suy luận trên bo |
| [docs/verification.md](docs/verification.md) | Những gì đã kiểm thử, kết quả, phần chưa kiểm chứng được |

## Phiên bản đã kiểm chứng

ESP-IDF **v5.5.6** (Docker `espressif/idf:v5.5.6`), `espressif/esp-tflite-micro` **1.4.1**, `espressif/esp-nn` **1.4.1** (khóa trong [`firmware/dependencies.lock`](firmware/dependencies.lock)). Python **3.12**: TensorFlow **2.21.0**, Keras 3.15.1, NumPy 2.5.4, pandas 3.1.0, scikit-learn 1.9.1, FastAPI 0.162.0, paho-mqtt 2.1.0, pyserial 3.5 ([`requirements/`](requirements)). Mosquitto **2.0.22**.
