# Home Assistant

Thiết bị dùng **MQTT**, không dùng Matter. Nó có thể kết nối thẳng vào broker của Home Assistant (add-on Mosquitto) và **tự xuất hiện** nhờ MQTT Discovery.

## Kết nối thiết bị vào MQTT của Home Assistant

1. Trong Home Assistant:
   - cài add-on **Mosquitto broker** và bật tích hợp **MQTT** (thường Home Assistant tự gợi ý);
   - tạo một người dùng riêng cho cảm biến: Cài đặt → Người & Nhóm → Người dùng → Thêm, ví dụ `cambien`.
2. Tìm IP của thiết bị trong danh sách khách của router. Thiết bị đặt hostname bằng mã của nó, ví dụ `s3-a1b2c3`.
3. Mở `http://<IP thiết bị>/`. Đây là trang cấu hình ngay trên ESP32, không cần console. Điền:
   - **Địa chỉ MQTT**: `mqtt://homeassistant.local:1883` (Home Assistant OS) hoặc `mqtt://<IP Home Assistant>:1883`;
   - **Tên đăng nhập / mật khẩu MQTT**: người dùng vừa tạo.
4. Bấm **Lưu và khởi động lại**. Khoảng 10 giây sau, trang hiện "MQTT: đã kết nối".

Thiết bị xuất hiện ở Cài đặt → Thiết bị & Dịch vụ → MQTT → **Cảm biến hiện diện s3-xxxxxx**:

| Thực thể | Loại | Nguồn |
|---|---|---|
| Có người | `binary_sensor`, `occupancy` | `decision` trong `presence/v1/dev/<id>/state`: `occupied` → bật, còn lại tắt |
| Trạng thái | `sensor` | Có người / Trống / Chưa chắc |
| Radar | `sensor`, chẩn đoán | `radar.status` trong `status` |
| Wi-Fi | `sensor`, chẩn đoán, dBm | `rssi` trong `status` |

- Khi thiết bị mất kết nối (LWT), mọi thực thể chuyển thành "không khả dụng".
- "Có người" dùng **quyết định đang giữ**, nên không nhấp nháy khi trạng thái tạm thời "chưa chắc".

Bản tin Discovery là `homeassistant/<loại>/<mã thiết bị>/<đối tượng>/config`, retained, gửi lại mỗi lần kết nối. Tắt bằng `CONFIG_PRESENCE_HA_DISCOVERY=n`; đổi tiền tố bằng `CONFIG_PRESENCE_HA_DISCOVERY_PREFIX`.

## Thu dữ liệu và huấn luyện khi dùng broker của Home Assistant

Backend của dự án có thể dùng chính broker đó. Trong `.env`:

```
MQTT_HOST=<IP Home Assistant>
MQTT_USERNAME=<một người dùng Home Assistant>
MQTT_PASSWORD=...
```

Khi đó không cần Mosquitto riêng (`deployment/docker-compose.yml`). Lưu ý: add-on Mosquitto cho mọi người dùng đã đăng nhập đọc/ghi mọi chủ đề. ACL theo từng thiết bị chỉ có trong Mosquitto riêng của dự án.

## Đã kiểm chứng

- Firmware trong QEMU, kết nối một broker yêu cầu đăng nhập và không có ACL (giống add-on của Home Assistant):
  - lưu cấu hình qua trang web;
  - thiết bị khởi động lại, kết nối, phát 4 bản tin Discovery.
- Template của các thực thể đã chạy thử bằng Jinja2 trên dữ liệu thật của thiết bị:
  - `occupied` → ON, `empty`/`unknown` → OFF;
  - LWT → không khả dụng.

**Chưa thử với Home Assistant thật và bo thật.**
