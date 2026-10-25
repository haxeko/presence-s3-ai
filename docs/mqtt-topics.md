# Chủ đề MQTT

Tiền tố mặc định `presence/v1`: trên thiết bị là `CONFIG_PRESENCE_TOPIC_PREFIX` (hoặc `config set topic_prefix` trên console), trên backend là `MQTT_TOPIC_PREFIX`. Mã thiết bị có dạng `s3-xxxxxx`, lấy 3 byte cuối MAC Wi-Fi và in ra log lúc khởi động.

| Chủ đề (sau `presence/v1/dev/<id>/`) | Chiều | QoS | Retained | Nội dung |
|---|---|---|---|---|
| `samples` | thiết bị → backend | 1 | không | Lô mẫu 5 Hz ([data-format.md](data-format.md)) |
| `status` | thiết bị → backend | 1 | **có** | Trạng thái đầy đủ, 15 s/lần và khi có thay đổi. LWT: `{"online":false,"reason":"lwt"}` |
| `state` | thiết bị → backend | 1 | **có** | Trạng thái hiện diện, gửi khi trạng thái thay đổi |
| `event` | thiết bị → backend | 1 | không | Tiến trình cập nhật mô hình, cấu hình bị từ chối, lệnh reboot |
| `desired/config` | backend → thiết bị | 1 | **có** | Cấu hình hiện diện, ngưỡng, nền |
| `desired/model` | backend → thiết bị | 1 | **có** | Mô hình mong muốn |
| `cmd` | backend → thiết bị | 1 | không | Lệnh một lần |

Backend đăng ký `presence/v1/dev/+/samples|status|state|event`.

## Kết nối của thiết bị

- `client_id` = mã thiết bị, keepalive 30 s, tự kết nối lại sau 5 s.
- Tên đăng nhập = `mqtt_user`; nếu để trống nhưng có mật khẩu thì dùng **mã thiết bị**, khớp ACL.
- `mqtts://...` dùng bộ chứng chỉ gốc của ESP-IDF (`CONFIG_PRESENCE_TLS_USE_CRT_BUNDLE`), xem [security.md](security.md).
- Mất mạng hoặc mất broker **không** ảnh hưởng việc phát hiện: radar, cửa sổ, mô hình và logic giữ trạng thái đều chạy trên thiết bị.

## `samples`: gửi, bộ đệm, chính sách bỏ mẫu

- Mỗi mẫu đi vào **bộ đệm vòng trong RAM** (mặc định 1000 mẫu = 200 s; dùng PSRAM nếu bo có). **Không ghi flash** theo từng mẫu.
- Bình thường, thiết bị gửi mỗi giây một lô (khoảng 5 mẫu). Khi còn tồn nhiều hơn `CONFIG_PRESENCE_BATCH_SAMPLES` (25), nó gửi liên tục, mỗi lô tối đa 25 mẫu, để xả hàng đợi sau khi mất mạng.
- Một mẫu chỉ bị xóa khỏi bộ đệm khi broker xác nhận (PUBACK). Mất kết nối, hoặc 30 s không có PUBACK, thì thiết bị gửi lại từ mẫu cũ nhất chưa được xác nhận. Backend bỏ mẫu trùng theo `(boot, seq)`.
- **Bộ đệm đầy thì bỏ mẫu CŨ NHẤT** (`buffer.policy = "drop_oldest"`): dữ liệu mới quan trọng hơn khi mạng trở lại. Số mẫu bị bỏ có trong `status.buffer.dropped`; trên backend chúng hiện thành khoảng trống `seq`.
- Đã đo trong QEMU: broker dừng 60 s, bộ đệm lên cao nhất 314 mẫu; khi broker chạy lại, `seq` 0–1345 về đủ, không thiếu mẫu nào.
- **Phía backend:** PUBACK chỉ nói broker đã nhận. Vì vậy backend dùng **phiên MQTT bền** (`clean_session=False`, `MQTT_CLIENT_ID` cố định). Khi backend khởi động lại hoặc mất kết nối, broker giữ các lô QoS 1 cho nó (`max_queued_messages 20000` trong `mosquitto.conf`) và giao lại khi nó quay về.
  - Đã thử: backend tắt 60 s giữa phiên thu, không thiếu mẫu nào.
  - Trước khi sửa, cùng phép thử mất khoảng 300 mẫu.
  - Mỗi `MQTT_CLIENT_ID` chỉ được một backend dùng.
- Kích thước đo từ phiên QEMU: khoảng 93 byte/mẫu, lô 5 mẫu khoảng 560 byte, lô 25 mẫu khoảng 2,4 KB. Mỗi thiết bị khoảng 2 MB/giờ.

## `status` (retained)

Ví dụ thật từ firmware chạy trong QEMU (đã rút gọn):

```json
{"online":true,"device_id":"s3-000000","fw":"0.1.0","fw_api":1,"idf":"v5.5.6","boot_id":2822111814,"boot_count":1,
 "uptime_s":430,"rssi":0,"ip":"10.0.2.15","time_synced":false,"heap_free":194204,"heap_min":141488,
 "radar":{"status":"ok","version":"V2.44.25070917","protocol":1,"gate_cm":75,"last_report_age_ms":35,"reports_ok":3965,
          "reports_other":0,"frames_bad":0,"bytes":178547,"uart_overflows":0,"engineering_attempts":4,"reinit_count":2,
          "max_moving_gate":8,"max_still_gate":8,"no_one_duration_s":5,
          "thresholds":{"moving":[50,50,40,30,20,15,15,15,15],"still":[0,0,40,40,30,30,20,20,20]}},
 "sampler":{"ticks":2149,"samples":1983,"empty_ticks":166,"queue_drops":0,"sample_rate_hz":4.614,"radar_reports_per_s":9.225},
 "buffer":{"capacity":1000,"used":2,"dropped":0,"high_water":314,"policy":"drop_oldest"},
 "model":{"method":"model",
          "active":{"model_id":"m20261025-150434","version":2,"slot":0,"simulated_data":true,"arena_used":3308,
                    "tflite_bytes":6888,"gate_cm":75,"recommended":{"enter":0.7,"exit":0.3,"confirm_occupied_ms":2000,"confirm_empty_ms":60000}},
          "update":{"phase":"active","model_version":2,"error":""},
          "slots":[{"state":"ready","version":2,"model_id":"m20261025-150434"},{"state":"ready","version":1,"model_id":"m20261025-145043"}],
          "previous":{"model_id":"m20261025-145043","version":1}},
 "presence":{"state":"occupied","decision":"occupied","method":"model","evidence":"high","score":0.859,
             "calibrated_probability":0.921,"tick":2144,"config_rev":0,
             "params":{"enter":0.7,"exit":0.3,"confirm_occupied_ms":2000,"confirm_empty_ms":60000},"prefer_threshold":false}}
```

- `radar.status`:
  - `ok`: có khung Engineering hợp lệ trong 2 s gần nhất (`CONFIG_PRESENCE_RADAR_TIMEOUT_MS`);
  - `init`: 5 s đầu sau khởi động;
  - `no_data`: không có khung hợp lệ (dây, nguồn, radar treo);
  - `not_engineering`: radar chỉ gửi khung cơ bản, không có năng lượng từng gate (bật Engineering Mode không thành công);
  - `unsupported_layout`: khung Engineering không đúng bố cục 9+9 gate;
  - `bad_frames`: có byte đến nhưng chưa có khung hợp lệ nào, thường do sai baud rate hoặc đấu nhầm chân.

  Khác `ok` thì thiết bị khởi tạo lại radar mỗi 10 s. Trong lúc đó trạng thái là `uncertain` và quyết định giữ nguyên. Radar mất **không bao giờ** được coi là phòng trống.
- `sampler.sample_rate_hz` là tốc độ mẫu thực tế, thấp hơn 5 khi có ô thiếu khung. `radar_reports_per_s` là tốc độ khung radar.
- `presence`: xem bảng bên dưới. Có thêm `params` (tham số giữ trạng thái đang dùng), `config_rev`, `baseline`, `prefer_threshold`. `model_blocked: "radar_gate_mismatch"` xuất hiện khi có mô hình nhưng radar đang báo độ phân giải gate khác mô hình, nên thiết bị dùng ngưỡng.

## `state` (retained)

```json
{"state":"occupied","decision":"occupied","method":"model","evidence":"high","score":0.859,"calibrated_probability":0.921,"tick":2144}
```

| Trường | Giá trị |
|---|---|
| `state` | `occupied`, `empty`, hoặc `uncertain`. Là `uncertain` khi: radar lỗi hoặc cửa sổ thiếu dữ liệu; chưa có quyết định nào; hoặc đang có bằng chứng ngược với quyết định nhưng chưa đủ thời gian xác nhận |
| `decision` | Quyết định đang giữ: `occupied`, `empty`, `unknown`. Chỉ đổi khi bằng chứng ngược kéo dài đủ `confirm_*_ms`. Khi `uncertain`, quyết định **giữ nguyên** giá trị trước đó |
| `method` | `model`, `threshold_baseline`, `threshold_factory` |
| `evidence` | `high` (≥ ngưỡng vào), `low` (≤ ngưỡng ra), `mid` (ở giữa), `none` (không có dữ liệu) |
| `score` | Điểm mô hình (tổng xác suất softmax của hai lớp có người), hoặc tỉ lệ mẫu vượt ngưỡng. **Không phải xác suất đã hiệu chuẩn** |
| `calibrated_probability` | Chỉ có khi gói mô hình có bảng hiệu chuẩn (isotonic trên tập validation) |

## `event`

```json
{"type":"model_update","phase":"active","model_version":2,"release_id":4,
 "selftest":{"vectors":24,"passed":true,"max_feature_diff":0,"max_input_diff":0,"max_output_diff":2,"inference_us_max":253,"arena_used":3308},"detail":""}
{"type":"model_update","phase":"failed","model_version":3,"release_id":5,"error":"selftest: output mismatch ..."}
{"type":"config","ok":false,"error":"invalid presence parameters or thresholds"}
{"type":"cmd","cmd":"reboot","ok":true}
```

`phase` lần lượt là `download` → `verify` → `active` hoặc `failed`. Backend lưu mọi sự kiện (bảng `events`, loại `device:<type>`) và cập nhật trạng thái bản phát hành theo `release_id`.

## `desired/config` (retained)

```json
{"rev":7,
 "presence_model":null,
 "presence_threshold":{"enter":0.5,"exit":0.2,"confirm_occupied_ms":2000,"confirm_empty_ms":60000},
 "thresholds":{"moving":[50,50,40,30,20,15,15,15,15],"still":[100,100,40,40,30,30,20,20,20]},
 "baseline":{"id":3,"name":"quạt số 2, đêm"},
 "prefer_threshold":false}
```

- `presence_model`: tham số giữ trạng thái khi dùng mô hình. `null` nghĩa là dùng tham số đề xuất trong gói mô hình.
- `presence_threshold`: tham số khi dùng phương pháp ngưỡng.
- `thresholds`: ngưỡng năng lượng 9 gate. Là ngưỡng xuất xưởng, hoặc ngưỡng tính từ phiên nền đang dùng (`baseline`). Gate 0–1 của ngưỡng tĩnh là 100 (tắt), vì LD2410 xuất xưởng không đo tĩnh ở đó.
- `prefer_threshold`: `true` thì dùng ngưỡng kể cả khi có mô hình.
- Ràng buộc: `0 ≤ exit < enter ≤ 1`, `confirm_occupied_ms ≤ 600000`, `1000 ≤ confirm_empty_ms ≤ 3600000`, ngưỡng gate trong 0–100.

Thiết bị kiểm tra **toàn bộ** cấu hình rồi mới áp dụng; sai thì giữ cấu hình cũ và gửi `event` `config`. Cấu hình hợp lệ được lưu NVS (chỉ khi có thay đổi), để vẫn áp dụng khi khởi động lại mà không có mạng.

## `desired/model` (retained)

```json
{"model_id":"m20261025-150434","model_version":2,"size":29824,"sha256":"<64 ký tự hex>","release_id":4,
 "path":"/api/device/models/m20261025-150434/package"}
```

- `model_id`: tối đa 32 ký tự `A–Z a–z 0–9 - _ .`.
- `path` chỉ để người đọc. Thiết bị tự ghép URL từ `server_url` đã cấu hình, nên một thông báo MQTT giả mạo **không** thể trỏ thiết bị tới máy chủ khác.
- Payload rỗng (retained) xóa yêu cầu. Thiết bị **giữ** mô hình đang chạy.

Quy trình xử lý: [model-update.md](model-update.md).

## `cmd`

```json
{"id":"3f9a1c2e","cmd":"reboot"}
```

| `cmd` | Tác dụng |
|---|---|
| `reboot` | Khởi động lại sau khi gửi `event` |
| `radar_reinit` | Khởi tạo lại radar (đọc cấu hình, bật Engineering Mode) |
| `ping`, `republish` | Gửi lại `status` ngay |

## Quyền trên broker (ACL)

[`deployment/mosquitto/config/acl`](../deployment/mosquitto/config/acl):
- Backend (`presence-backend`) có toàn quyền trên `presence/v1/#`.
- Mỗi thiết bị (tên đăng nhập = mã thiết bị) chỉ **ghi** được `samples`, `status`, `state`, `event` của chính nó, và chỉ **đọc** được `desired/#` và `cmd` của chính nó.

Vì vậy, một thiết bị bị lộ mật khẩu không đọc hay ghi được thiết bị khác, và không tự đặt được `desired/model` hay `cmd`. Kiểm tra bằng broker thật: `sh deployment/scripts/test-mosquitto-acl.sh` (cần Docker; có trong CI).
