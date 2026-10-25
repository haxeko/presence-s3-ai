# Đo RAM, flash, thời gian suy luận trên bo

**Chưa đo trên bo ESP32-S3 thật.** Các số dưới đây lấy từ `idf.py size` (tĩnh, chính xác cho bản build) và từ QEMU. Trong QEMU, mạng là OpenETH thay cho Wi-Fi, và **thời gian chạy không đại diện cho chip thật**: QEMU không mô phỏng đúng tốc độ CPU, cache flash hay PSRAM. Hãy đo lại trên bo theo các bước ở cuối trang và ghi kết quả vào [verification.md](verification.md).

## Số đã có

| Hạng mục | Giá trị | Nguồn |
|---|---|---|
| Ảnh firmware `presence_s3_ai.bin` | khoảng 1,09 MB, còn 45% trống trong phân vùng `factory` 2 MB | `idf.py size` |
| RAM tĩnh (DIRAM) | khoảng 130 KB / 341 KB (38%) | `idf.py size` |
| Bộ đệm mẫu offline | 1000 mẫu × 60 byte = 60 000 byte (RAM trong) | log khởi động |
| Mô hình 72-32-16-4 đang chạy | 12 648 byte heap: flatbuffer 6 888 + arena 3 308 + engine | test trên QEMU |
| Lúc cài mô hình (tạm thời) | đọc cả gói (~30 KB) vào RAM + engine thứ hai; cần dư ít nhất 48 KB heap (`CONFIG_PRESENCE_MODEL_HEAP_RESERVE_KB`) | thiết kế |
| Heap còn trống khi chạy (QEMU, OpenETH, MQTT TLS, mô hình đang chạy) | 167 KB; thấp nhất từ lúc khởi động 126 KB; khối liên tục lớn nhất 80 KB | lệnh `mem` |
| Một lần quyết định (đặc trưng + lượng tử + suy luận + logic) | 37–254 µs thường gặp, lớn nhất 4 960 µs (QEMU) | `status.presence.eval_us`, `mem` |
| Riêng suy luận TFLM (24 vector kiểm tra) | 158–15 679 µs trong QEMU, dao động rất mạnh | sự kiện `model_update` → `selftest.inference_us_max` |

Chồng (stack) còn trống thấp nhất của từng task, đo trong QEMU (lệnh `mem`), **trước** khi tăng stack:

| Task | Stack | Còn trống thấp nhất |
|---|---|---|
| `radar_rx` | 4 096 | 3 052 |
| `radar_ctl` | 4 096 | 1 708 |
| `decision` | 6 144 | 2 932 |
| `net` | 6 144 | 2 124 |
| `model_upd` | 6 144 | **936** (sau khi tải HTTPS + ECDSA) → đã tăng lên 8 192 |
| `sys_evt` | 2 304 | **440** → đã tăng lên 4 096 (`CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE`) |
| `mqtt_task` | mặc định | 2 988 |
| `tiT` (lwIP) | mặc định | 1 152 |

## Đo trên bo

### 1. Flash và RAM tĩnh
```powershell
idf.py size              # tổng: flash code/data, DIRAM, IRAM
idf.py size-components   # theo thành phần (TFLM, mbedTLS, Wi-Fi...)
```

### 2. Heap và stack khi chạy
Mở console (`idf.py -p COM5 monitor`), gõ:
```
presence> mem
internal RAM: free 167128, lowest since boot 126540, largest block 81920 bytes
window evaluation: last 254 us, slowest 4960 us
stack free radar_rx        3052 bytes
...
```
- `lowest since boot` là mức heap thấp nhất từ lúc khởi động. Để chạy **24 giờ**, có ít nhất một lần cập nhật mô hình qua mạng, rồi đọc lại. Con số phải ổn định (không giảm dần theo thời gian) và nên còn > 40 KB.
- Stack còn trống dưới khoảng 512 byte thì tăng stack của task đó: `xTaskCreatePinnedToCore(...)` trong `firmware/main/*.cpp`, hoặc Kconfig với task của hệ thống.
- Có PSRAM thì `mem` in thêm dòng PSRAM, và bộ đệm mẫu nằm trong PSRAM.

Cùng các số này có trong `status` gửi qua MQTT và trên dashboard (tab Tổng quan):
- `heap_free`, `heap_min`;
- `presence.eval_us`;
- `model.active.arena_used`, `model.active.tflite_bytes`.

### 3. Thời gian suy luận
- Mỗi lần cài mô hình, thiết bị đo thời gian `Invoke` trên 24 vector kiểm tra. Kết quả nằm trong sự kiện `model_update`: `selftest.inference_us_max`. Dashboard → tab Mô hình → Lịch sử phát hành → chi tiết.
- `presence.eval_us.max` là thời gian lâu nhất của cả bước quyết định mỗi giây. Phải nhỏ hơn nhiều so với 200 ms (một tick). Nếu không, task `decision` sẽ làm đầy hàng đợi tick (`sampler.queue_drops` tăng).

### 4. Tốc độ radar, mất byte UART
Trong `status`:
- `sampler.radar_reports_per_s` khoảng 10 (tùy radar);
- `sampler.sample_rate_hz` khoảng 5,0;
- `radar.uart_overflows = 0`;
- `radar.frames_bad` không tăng.

Thử thêm trong lúc **cập nhật mô hình** (ghi flash): `uart_overflows` phải vẫn bằng 0, vì ISR UART nằm trong IRAM.

### 5. Mất mạng
- Tắt broker 5 phút. Bộ đệm 1000 mẫu chỉ giữ được 200 s, nên `buffer.dropped` phải tăng khoảng 500. Trên backend sẽ có khoảng trống `seq` tương ứng; đó là hành vi đúng của chính sách "bỏ mẫu cũ nhất".
- Tắt backend (broker vẫn chạy) vài phút: không được mất mẫu, nhờ phiên MQTT bền.

### 6. Điện năng (chưa có số)
Đo bằng đồng hồ USB hoặc điện trở shunt ở đường 5 V, khi Wi-Fi kết nối và radar chạy. Firmware không dùng chế độ ngủ: radar và Wi-Fi luôn bật.
