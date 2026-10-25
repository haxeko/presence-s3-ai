# Đấu nối ESP32-S3-DevKitC-1 với HLK-LD2410 / LD2410B / LD2410C

| LD2410 (chữ in trên mạch) | ESP32-S3-DevKitC-1 | Ghi chú |
|---|---|---|
| **VCC** | **5V** | Radar cần 5 V (khoảng 80 mA khi hoạt động). Chân 5V của DevKitC-1 có điện khi cắm USB |
| **GND** | **GND** | Nối đất chung |
| **TX** | **GPIO18** (RX của UART1) | Mức logic 3,3 V, nối thẳng |
| **RX** | **GPIO17** (TX của UART1) | Mức logic 3,3 V, nối thẳng |
| OUT | không nối (tùy chọn GPIO khác) | Firmware đọc trạng thái qua UART |

- UART1, **256000 baud, 8N1** (mặc định LD2410). Firmware không đổi baud của radar. Nếu radar đã được đổi baud bằng HLKRadarTool, đặt lại bằng console: `config set baud 115200`, rồi `reboot`.
- **Vì sao chọn GPIO17/18:** đó là chân UART1 mặc định trong IO MUX của ESP32-S3, và tránh được các chân sau:
  - chân strapping 0, 3, 45, 46;
  - USB 19/20;
  - flash/PSRAM 26–32;
  - PSRAM octal 33–37 (trên bản N8R8/N16R8);
  - UART0 43/44 (console);
  - LED RGB (GPIO48 trên DevKitC-1 v1.0, GPIO38 trên v1.1).
- Đổi chân bằng `idf.py menuconfig` → *Presence S3 AI → LD2410 radar*, hoặc lúc chạy bằng console (`config set rx_gpio 16`, `config set tx_gpio 15`, `reboot`).
- Cổng **UART** của DevKitC-1 (qua chip USB-UART) là cổng nạp, log và console cấu hình (115200 baud). Cổng **USB** (native) không dùng.
- Chân ra hiện diện (tùy chọn): `PRESENCE_OUTPUT_GPIO` trong menuconfig, mức cao khi quyết định là "có người" (ví dụ nối LED hoặc relay qua transistor). Chân này giữ nguyên lúc khởi động cho tới quyết định đầu tiên.

## Lắp đặt radar

- Mặt anten hướng vào vùng cần theo dõi. Radar xuyên qua nhựa và gỗ mỏng nhưng không qua kim loại.
- Mỗi gate dài 0,75 m (gate 0: 0–0,75 m, …, gate 8: 6–6,75 m). Ghi vị trí quạt, rèm, ghế ngồi theo gate trong "ghi chú môi trường" của phiên thu.
- Cố định radar trước khi thu dữ liệu. Dời radar hoặc đổi hướng thì phải thu và huấn luyện lại, vì dữ liệu nền đã khác.

## Phát lại dữ liệu radar khi chưa có radar

Dùng bộ chuyển USB-UART 3,3 V (CP2102, CH340…):

| USB-UART | ESP32-S3 |
|---|---|
| TX | GPIO18 (RX radar) |
| RX | GPIO17 (TX radar), để công cụ trả lời lệnh cấu hình |
| GND | GND |

```powershell
.\.venv\Scripts\python.exe tools\ld2410_replay.py --serial COM7 --scenario mix
```

Bộ chuyển phải hỗ trợ 256000 baud (CP2102/CH340 có hỗ trợ). Khung phát ra là **mô phỏng**, chỉ dùng để thử đường dữ liệu. Muốn phát lại dữ liệu thật thì trước đó thu bằng `tools/ld2410_capture.py` khi có radar (bộ chuyển nối thẳng với radar).
