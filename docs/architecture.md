# Kiến trúc

```
 LD2410 ──UART 256000──► ESP32-S3 ──MQTT (mẫu 5 Hz, trạng thái, sự kiện)──► Mosquitto ──► Backend FastAPI ──► SQLite + CSV
                          │  ▲                                                        │        ▲
                          │  └──── HTTP(S) tải gói mô hình (token thiết bị) ◄─────────┤        │ tác vụ huấn luyện (tiến trình con)
                          │         desired/model, desired/config (retained MQTT) ◄────┘        │
                          └─ phát hiện tại chỗ (mô hình INT8 hoặc ngưỡng) ──► GPIO tùy chọn    presence_ml: train → INT8 → đánh giá → gói ký
                                                                                       Dashboard (trình duyệt) ──► /api
```

## Firmware ESP32-S3

| Task | Ưu tiên, lõi | Việc |
|---|---|---|
| `radar_rx` | 12, lõi 1 | Đọc UART (ring buffer 4 KB, ISR trong IRAM), bộ phân tích LD2410 theo luồng, đưa khung Engineering đủ 9+9 gate vào bộ gom 5 Hz |
| `esp_timer` "tick" | (task esp_timer) | Mỗi 200 ms đóng một ô, gửi `TickMsg` (có hoặc không có mẫu) sang task quyết định |
| `decision` | 8, lõi 1 | Cửa sổ 20 tick → mô hình (đặc trưng → chuẩn hóa → int8 → TFLM) hoặc phương pháp ngưỡng → logic giữ trạng thái → gắn kết quả vào mẫu → bộ đệm offline; chân GPIO hiện diện (tùy chọn) |
| `radar_ctl` | 5, lõi 1 | Đọc cấu hình radar (0x0061, 0x00A0, 0x00AB), bật Engineering Mode (0x0062), xác nhận bằng báo cáo thực nhận. Mất dữ liệu > 2 s → báo lỗi riêng, khởi tạo lại 10 s/lần. Chưa đọc được cấu hình (radar bật sau ESP32) → đọc lại 10 s/lần; trong lúc đó mô hình tạm dừng vì chưa biết độ phân giải gate |
| `net` | 4, lõi 0 | Wi-Fi/SNTP/MQTT; gửi lô mẫu QoS 1, chỉ xóa khỏi bộ đệm khi broker đã PUBACK; trạng thái 15 s/lần; nhận cấu hình và mô hình mong muốn |
| `model_upd` | 3, lõi 0 | Tải gói vào vùng A/B không hoạt động, kiểm tra, chạy thử, kích hoạt |

Nguyên tắc:

- **Không để mạng làm mất dữ liệu radar.** Phần đọc UART chạy trên lõi 1 với ưu tiên cao, ISR UART nằm trong IRAM nên vẫn nhận byte trong lúc ghi flash (tải mô hình, NVS). Chồng ngăn xếp mạng chạy trên lõi 0. Task quyết định giữ khóa mô hình trong suốt một bước, nên không bao giờ thấy một mô hình đang chuyển dở.
- **Mất dữ liệu không phải phòng trống.** Ô không có khung hợp lệ là "thiếu dữ liệu", không lặp lại mẫu cũ. Cửa sổ thiếu quá nhiều sẽ không được chấm điểm. Logic hiện diện chuyển sang "chưa chắc" và giữ quyết định trước đó. Radar báo `no_data`, `not_engineering`, `bad_frames` hoặc `unsupported_layout` trong trạng thái thiết bị.
- **Bộ đệm offline có giới hạn, chỉ nằm trong RAM.** Mặc định 1000 mẫu (200 s, 60 KB). Có PSRAM thì dùng PSRAM và có thể lớn hơn nhiều. Khi đầy thì **bỏ mẫu cũ nhất** và đếm số mẫu bị bỏ (`buffer.dropped`); backend thấy khoảng trống `seq`. Không ghi flash theo từng mẫu: NVS chỉ ghi khi cấu hình đổi, phân vùng mô hình chỉ ghi khi cập nhật.
- **Phát hiện tại chỗ không cần máy chủ.** Mô hình và cấu hình (lưu NVS) chạy khi mất mạng. Chân `PRESENCE_OUTPUT_GPIO` (tùy chọn) phản ánh quyết định.

## Backend

- **Nhận mẫu:** cầu nối MQTT (paho-mqtt) dùng **phiên bền**, nên broker giữ mẫu khi backend khởi động lại. Backend bỏ mẫu trùng theo `(boot, seq)` và tính giờ thực ([data-format.md](data-format.md)). Mẫu được ghi CSV nếu thiết bị đang có phiên thu, đồng thời giữ 15 phút trong bộ nhớ cho biểu đồ trực tiếp.
- **SQLite (WAL)** chứa metadata: thiết bị, phòng, phiên, nhãn, sự kiện, nền, tác vụ, mô hình, phát hành, đồng hồ thiết bị. Chuỗi mẫu nằm trong file CSV riêng của từng phiên.
- **Huấn luyện** chạy như tiến trình con (`python -m presence_ml.cli train`), mỗi lúc một tác vụ. Tiến độ được đọc từ dòng `PROGRESS {json}`. Backend không cần TensorFlow trong tiến trình web.
- **Phát hành:** chỉ mô hình đã duyệt. Backend ghi `desired/model` (retained) và theo dõi tiến trình qua `event` của thiết bị.

## Phương pháp phát hiện đang dùng

| Ưu tiên | Phương pháp (`method`) | Khi nào |
|---|---|---|
| 1 | `model` | Có mô hình hợp lệ, radar đã báo độ phân giải gate và khớp hợp đồng, không bật "ưu tiên ngưỡng" |
| 2 | `threshold_baseline` | Không dùng mô hình và người dùng đã chọn một phiên nền làm ngưỡng |
| 3 | `threshold_factory` | Mặc định: ngưỡng xuất xưởng LD2410 (tĩnh gate 0-1 tắt) |

Dashboard hiển thị phương pháp đang dùng của từng thiết bị. Điểm của phương pháp ngưỡng là tỉ lệ mẫu trong cửa sổ có gate vượt ngưỡng. Điểm mô hình là tổng softmax của hai lớp có người. **Cả hai đều không phải xác suất.** Nếu gói mô hình có bảng hiệu chuẩn (học trên validation), trạng thái hiển thị thêm "xác suất hiệu chuẩn ước lượng".

## Logic giữ trạng thái (giống hệt Python ↔ C++)

Mỗi giây (hết một hop):

- **Mức bằng chứng:** CAO khi điểm ≥ `enter`, THẤP khi điểm ≤ `exit`, còn lại là GIỮA, hoặc KHÔNG CÓ khi thiếu dữ liệu.
- **Quyết định:**
  - chuyển sang "có người" khi CAO liên tục ≥ `confirm_occupied_ms` (mặc định 2 s);
  - chuyển sang "trống" khi THẤP liên tục ≥ `confirm_empty_ms` (mặc định 60 s);
  - mức GIỮA hoặc KHÔNG CÓ đặt lại cả hai bộ đếm.
- **Trạng thái báo cáo** là "chưa chắc" khi: chưa có quyết định nào, thiếu dữ liệu, hoặc đang có bằng chứng ngược chiều nhưng chưa đủ thời gian xác nhận. Trong các trường hợp đó, quyết định trước được giữ nguyên.

Mặc định của mô hình: `enter` 0,7, `exit` 0,3. Mặc định của phương pháp ngưỡng: 0,5 / 0,2. Cả hai **là giá trị khởi đầu, chưa được hiệu chỉnh bằng dữ liệu thật**. Chỉnh được trên dashboard (tab Cài đặt); gói mô hình mang tham số đề xuất của lần huấn luyện.

## Học phòng trống

Người dùng chủ động kích hoạt và phải xác nhận phòng không có người. Backend ghi một phiên `baseline` có nhãn trống (ít nhiễu hoặc có nhiễu), rồi tính thống kê theo 18 kênh (trung bình, độ lệch chuẩn, p50/p95/p99, max). Nếu thấy dấu hiệu có người thì cảnh báo: năng lượng tĩnh > 60 kéo dài, radar tự báo mục tiêu tĩnh, hoặc mô hình trên thiết bị kết luận có người. Nền **không bao giờ tự kích hoạt**. Khi người dùng chọn "Dùng làm nền", ngưỡng `đỉnh + max(8, đỉnh − trung bình)` được gửi xuống làm phương pháp ngưỡng đối chứng. Hợp đồng 72 đặc trưng của mô hình không thay đổi. Muốn thêm đặc trưng "sai lệch so với nền" thì phải tạo hợp đồng v2 (phiên bản xử lý đầu vào mới), huấn luyện lại và nâng `kFirmwareModelApi`.

## Giả định đã chọn (khi yêu cầu chưa nói rõ)

| Giả định | Lý do / cách đổi |
|---|---|
| Bo ESP32-S3-DevKitC-1 N8 (flash 8 MB, không PSRAM); bảng phân vùng vừa cả flash 4 MB | Phổ biến; có PSRAM thì bộ đệm tự dùng PSRAM |
| UART1, RX = GPIO18 ← TX radar, TX = GPIO17 → RX radar | Chân UART1 mặc định của ESP32-S3, không phải chân strapping/flash/USB/PSRAM octal. Đổi bằng `menuconfig` hoặc console (`config set rx_gpio ...`) |
| Mẫu 5 Hz = **giá trị lớn nhất** theo kênh của các khung trong ô 200 ms | Giữ được xung chuyển động ngắn; thiết bị và máy tính dùng chung quy ước |
| Gate 0,75 m (mặc định LD2410) | Gói mô hình ghi độ phân giải gate; radar báo khác thì mô hình không chạy |
| Thiết bị không ghi cấu hình vào radar (ngưỡng, gate, baud) | Mọi phiên thu giữ đúng cấu hình radar đã báo; tránh mòn flash radar |
| Mã thiết bị `s3-` + 3 byte cuối MAC; tên đăng nhập MQTT mặc định = mã thiết bị | Khớp ACL Mosquitto (`pattern write/read presence/v1/dev/%u/...`) |
| Múi giờ Việt Nam khi chia tập "theo ngày" | `presence_ml/dataio.py` (`SessionMeta.day`) |
| Không có OTA firmware | Yêu cầu chỉ nói cập nhật mô hình; bảng phân vùng chỉ có `factory` |
