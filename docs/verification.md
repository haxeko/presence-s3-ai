# Kiểm chứng: đã thử gì, kết quả, phần chưa kiểm chứng được

Mọi kết quả dưới đây là lần chạy thật trên Linux x86_64: Python 3.12.3, Docker, `espressif/idf:v5.5.6`, QEMU ESP32-S3 của Espressif. Không có bo ESP32-S3 hay radar LD2410 thật. **Không có số liệu nào về khả năng phát hiện người thật.**

## 1. Test tự động

| Bộ test | Lệnh | Kết quả |
|---|---|---|
| Lõi firmware C++ trên máy tính (crypto OpenSSL) | `cmake -S firmware/test/host -B build/host -G Ninja && cmake --build build/host && build/host/host_tests` | **5 591 phép kiểm, 0 lỗi** |
| Như trên, crypto bằng mbedTLS của ESP-IDF (cùng mã với ESP32) | `-DPRESENCE_CRYPTO=mbedtls` trong `espressif/idf:v5.5.6` | **5 591 / 0 lỗi** |
| Python: huấn luyện (20), backend (11), công cụ cổng COM (2) | `pytest` | **33 / 33 đạt** |
| Firmware ESP32-S3 (build sạch) | `idf.py build && idf.py size` | Không cảnh báo từ mã dự án. Ảnh 1 147 659 byte (còn 45% của 2 MB), DIRAM 130 835 / 341 760 byte (38,3%) |
| Test trên chip ảo (QEMU ESP32-S3) | `firmware/test/qemu/run_target_test.sh` | **PASS, 0 lỗi** (chi tiết bên dưới) |
| ACL Mosquitto với broker thật | `sh deployment/scripts/test-mosquitto-acl.sh` | **8 / 8 đạt**, cả khi chạy bằng root lẫn bằng người dùng thường như runner của GitHub. Với ACL cũ (`readwrite dev/%u/#`) test báo hỏng 2 mục, tức test phân biệt được |

### Các test quan trọng đã có

**Parser LD2410** (C++ và Python dùng chung 14 ca, đưa vào theo mảnh ngẫu nhiên):
- khung bị chia nhỏ hoặc dính liền;
- rác giữa các khung;
- sai header/footer/0xAA/0x55/byte kiểm tra;
- LEN quá lớn;
- khung thật nằm trong khung hỏng;
- số gate khác 8.

**Đặc trưng:**
- 72 đặc trưng Python so với một bản tính độc lập bằng `fractions.Fraction`;
- C++ trên máy tính và trên ESP32-S3 (QEMU) cho kết quả **giống từng bit** với Python trên 24 cửa sổ, có cả cửa sổ thiếu ô.

**Chia dữ liệu:**
- không phiên/ngày nào nằm ở hai tập;
- chuẩn hóa chỉ học từ tập train;
- cửa sổ không vượt qua lần khởi động, không vượt qua điểm đổi nhãn.

**INT8:**
- 24 vector kiểm tra trong gói: đầu vào int8 lệch 0;
- đầu ra lệch ≤ 2 LSB giữa esp-nn trên ESP32-S3 và kernel tham chiếu của TFLite. Ngưỡng cho phép mặc định là 3.

**Gói mô hình:**
- sửa 1 byte bất kỳ thì bị từ chối;
- ký bằng khóa khác thì bị từ chối;
- sai `fw_api`, sai độ phân giải gate, hợp đồng không hợp lệ thì bị từ chối.

**Cập nhật bị ngắt (mất điện):**
- mô phỏng mất điện tại **793** thời điểm khác nhau của một lần cập nhật;
- sau mỗi lần khởi động lại đều chạy một mô hình đã kiểm chứng: 792 lần giữ v1, 1 lần là v2, khi bản ghi kích hoạt đã ghi xong;
- bản ghi metadata ghi dở bị bỏ qua nhờ CRC-32.

**Kiểm tra nhất quán thiết bị ↔ máy tính** (`presence_ml.cli consistency`) phát hiện được cửa sổ bị sửa.

**Công cụ cổng COM:**
- `ld2410_replay.py --serial` và `ld2410_capture.py --serial` chạy qua cặp pty ảo;
- thu được file, phát lại được, có trả lời lệnh cấu hình.

### Test trên QEMU ESP32-S3 (`run_target_test.sh`)

```
features: 24 windows, 0 values differ from Python
package sim-fixture-v1 v1: 29808 bytes, sha256 + ECDSA verify 374014 us
  ok   tampered package rejected
install v1: ok, selftest 24 vectors, max diff feature 0 input 0 output 1, inference max 4948 us, arena 3308 bytes
heap used by the active model: 12648 bytes (package + arena + interpreter)
install v2: ok, selftest 24 vectors, max diff feature 0 input 0 output 0
  ok   rollback to v1 without download
  ok   corrupted previous slot refused, v2 kept
  ok   boot loads the active slot (v2)
PRESENCE TARGET TEST PASS (0 failures)
```

## 2. Chạy toàn hệ thống với firmware THẬT trong QEMU

Thiết lập:
- firmware `main` build cho QEMU, dùng OpenETH thay cho Wi-Fi;
- UART1 (radar) nối qua TCP với `tools/ld2410_replay.py`, phát khung LD2410 **mô phỏng** và trả lời lệnh cấu hình;
- Mosquitto 2.0.22 với ACL của repo;
- backend chạy trên máy.

### Lần 1: MQTT không TLS, HTTP

| Kiểm tra | Kết quả |
|---|---|
| Khởi tạo radar | `V2.44.25070917, protocol 1, gate 75 cm, engineering on, config closed` |
| Phiên thu trên dashboard | 146 mẫu ghi CSV, nhãn theo thời gian |
| Huấn luyện → duyệt → phát hành → tải → kiểm tra → kích hoạt | Mô hình v2 đang chạy. Tự kiểm 24 vector: đặc trưng lệch 0, đầu vào lệch 0, đầu ra lệch 2 |
| Phát hành v1 rồi khôi phục v2 | Khôi phục qua bước kiểm tra, không tải lại |
| Broker tắt 60 s | Bộ đệm lên cao nhất 314 mẫu. Sau khi broker chạy lại, `seq` 0–1345 đủ, không thiếu. Bước thời gian 179–223 ms (trung vị 200) |
| Radar im lặng | `radar: no_data`, trạng thái `uncertain`, **giữ quyết định trước**, không coi là trống. Radar chạy lại thì tự khởi tạo lại |
| Kịch bản "người ngồi yên" | Mô hình (huấn luyện bằng dữ liệu mô phỏng) báo có người, điểm 0,83 |

### Lần 2: TLS (`mqtts://` 8883, `https://` 8443, CA tự tạo nhúng vào firmware)

| Kiểm tra | Kết quả |
|---|---|
| MQTT qua TLS với CA riêng | Kết nối được; `status.tls_trust = "private_ca"` |
| Backend (paho) kết nối broker qua TLS với `MQTT_CA_FILE` | Kết nối được |
| Tải gói qua HTTPS + token thiết bị | v2 rồi v1: tải → kiểm tra → kích hoạt, tự kiểm 24 vector |
| **Âm tính:** backend dùng chứng chỉ của CA khác | Thiết bị từ chối: `mbedtls_ssl_handshake returned -0x2700` (lỗi xác minh X.509). Không có request nào tới backend. Mô hình đang chạy giữ nguyên |
| **Âm tính:** broker dùng chứng chỉ của CA khác | Thiết bị từ chối (`-0x2700`). Khôi phục chứng chỉ đúng thì kết nối lại |
| **Backend tắt 60 s giữa phiên thu** | Trước khi sửa: mất khoảng 300 mẫu, và giờ của các lô đến trễ lệch tới 60 s. Sau khi sửa (phiên MQTT bền + ước lượng đồng hồ thiết bị): 674 mẫu, `seq` liền nhau, không trùng, bước thời gian 193–201 ms |
| **Nhất quán thiết bị ↔ máy tính** trên dữ liệu thật của phiên đó (thiết bị chạy mô hình v1) | **131 cửa sổ so sánh, 0 lệch, sai khác lớn nhất 2 LSB.** 4 cửa sổ đầu phiên bị bỏ qua vì chúng gồm cả ô trước lúc bắt đầu thu. Lỗi này của công cụ kiểm tra được phát hiện và sửa nhờ chính lần chạy này |
| Radar gửi khung nhưng **không trả lời lệnh** lúc khởi động | Trước khi sửa: không bao giờ đọc lại cấu hình, `gate_cm = 0`, và mô hình vẫn chạy mà không kiểm tra độ phân giải. Sau khi sửa: đọc lại 10 s/lần; mô hình đã cài nhưng **tạm dừng** (`model_blocked: radar_gate_unknown`) và dùng ngưỡng; radar trả lời thì gate 75 cm và mô hình chạy lại |
| Lệnh console `mem` | Heap trống 163–167 KB, thấp nhất 122–126 KB. Stack còn trống của từng task: [hardware-measurements.md](hardware-measurements.md). Phát hiện `model_upd` chỉ còn 936 B và `sys_evt` 440 B; đã tăng stack và đo lại (2 980 B và 2 232 B) |
| Dashboard qua HTTPS (Playwright) | Hiện đủ trường mới (thời gian quyết định, mô hình tạm dừng). Không có lỗi JS, ngoài 401 trước khi nhập token |

Log thu được trong lúc chạy không nằm trong repo. Có thể tái tạo bằng `firmware/test/qemu/run_firmware_e2e.sh` (xem đầu file).

## 3. Khác

- **Image Docker của backend** (`deployment/backend.Dockerfile`):
  - đã build và chạy được, trong container: `/api/health` kết nối MQTT, dashboard trả 200, nhập dữ liệu mô phỏng, chạy một tác vụ huấn luyện TensorFlow trọn vẹn. Mô hình được đánh dấu đúng là mô phỏng / chưa đủ dữ liệu;
  - riêng trong môi trường thử, proxy giải mã TLS nên phải thêm CA của proxy vào bản sao Dockerfile dùng khi build. File trong repo không đổi. Image không chứa `.env`, `secrets/`, `data/` (có `.dockerignore`).
- **Mosquitto với `mosquitto.conf` của repo** chạy được. Script tạo người dùng `mosquitto-user.sh` chạy được.
- **Huấn luyện bằng dữ liệu mô phỏng:**
  - pipeline chạy hết khoảng 11 s; `model.tflite` 6,9 KB; gói khoảng 30 KB;
  - báo cáo ghi rõ "MÔ PHỎNG" và "CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN";
  - mô hình mô phỏng yếu ở lớp `occupied_still`, và báo cáo ghi đúng như vậy. Đây chỉ là kiểm tra pipeline.

## 4. Chưa kiểm chứng được (cần phần cứng hoặc môi trường khác)

| Hạng mục | Vì sao | Cách kiểm |
|---|---|---|
| Bo ESP32-S3 thật: UART 256 000 baud, ngắt UART trong lúc ghi flash, Wi-Fi, thời gian thật, RAM thật, điện năng | Không có phần cứng. QEMU dùng OpenETH, không mô phỏng Wi-Fi, và thời gian trong QEMU không đại diện | [hardware-measurements.md](hardware-measurements.md) |
| Radar LD2410/B/C thật: định dạng khung Engineering, byte phụ, lệnh 0x00AB, cách radar dừng gửi khi ở chế độ cấu hình | Mã viết theo tài liệu Hi-Link; chỉ thử bằng khung tự tạo | [ld2410.md](ld2410.md#kiểm-tra-radar-thật-khi-có-phần-cứng) |
| Khả năng phát hiện người thật, tỉ lệ báo giả/bỏ sót, độ trễ vào/ra | Không có dữ liệu thật. Mọi mô hình trong repo dùng dữ liệu mô phỏng | Thu dữ liệu ([data-collection.md](data-collection.md)), huấn luyện, đọc báo cáo |
| Ngưỡng mặc định (`enter`/`exit`, thời gian xác nhận) | Chưa hiệu chỉnh bằng dữ liệu thật | Chỉnh trên dashboard sau khi có báo cáo |
| Windows: `setup-windows.ps1`, `mosquitto-user.ps1`, Docker Desktop, ESP-IDF trên Windows | Môi trường thử là Linux | Làm theo [windows-setup.md](windows-setup.md) |
| Đường Wi-Fi của firmware (kết nối lại có lùi thời gian, RSSI) | Đã biên dịch nhưng chưa chạy | Trên bo thật |
| ESP-IDF khác v5.5.6 | `idf_component.yml` giới hạn `>=5.5,<5.6` | |
| Flash Encryption, NVS Encryption, Secure Boot | Chưa bật | [security.md](security.md) |
| Thiết bị không kiểm tra hạn chứng chỉ TLS | Hạn chế đã biết của cấu hình mbedTLS mặc định | [security.md](security.md#tls) |
| Mosquitto ≥ 2.1 với file `passwd` có quyền đọc rộng (thư mục mount từ Windows) | 2.0.22 chỉ cảnh báo | [security.md](security.md#quyền-file-của-mosquitto) |
