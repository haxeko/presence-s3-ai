# API HTTP của backend

Backend là FastAPI ([`backend/presence_server/api.py`](../backend/presence_server/api.py)). Dashboard nằm ở `/dashboard/` và chỉ dùng các API dưới đây. Tài liệu tương tác (OpenAPI) ở `/docs`.

## Xác thực

| Loại | Biến môi trường | Gửi kèm | Dùng cho |
|---|---|---|---|
| Quản trị | `ADMIN_TOKEN` | `Authorization: Bearer <token>` hoặc `X-Admin-Token: <token>` | Mọi `/api/*`, trừ `/api/health` và `/api/device/*` |
| Thiết bị | `DEVICE_TOKENS` (nhiều token, cách nhau bởi dấu phẩy) | `Authorization: Bearer <token>` | `/api/device/models/{id}/package` |

- Token được so sánh trong thời gian hằng (`hmac.compare_digest`).
- **Để trống `ADMIN_TOKEN` thì API không có xác thực.** Backend ghi cảnh báo lúc khởi động. Chỉ để trống khi thử trên máy hoặc trong LAN tin cậy, xem [security.md](security.md).
- Trên dashboard, token quản trị nhập ở tab **Cài đặt** và chỉ lưu trong `localStorage` của trình duyệt.

Lỗi trả về `{"detail": "<thông báo tiếng Việt>"}` với mã 400 (yêu cầu sai), 401 (thiếu hoặc sai token), 403, 404 hoặc 409 (xung đột trạng thái).

## Danh sách

### Chung
| Phương thức | Đường dẫn | Mô tả |
|---|---|---|
| GET | `/api/health` | `{"ok", "mqtt_enabled", "mqtt_connected", "admin_auth", "device_auth"}`, không cần token |
| GET | `/api/contract` | Hợp đồng đầu vào v1: tên 72 đặc trưng, kênh, lớp, cửa sổ, chu kỳ |

### Thiết bị
| Phương thức | Đường dẫn | Thân yêu cầu / tham số | Mô tả |
|---|---|---|---|
| GET | `/api/devices` | | Danh sách thiết bị với `status` gần nhất (từ MQTT) |
| GET | `/api/devices/{id}` | | Một thiết bị |
| PATCH | `/api/devices/{id}` | `{"name"?, "room_id"?}` | Đổi tên, gán phòng |
| GET | `/api/devices/{id}/live` | `?cursor=0&limit=1500` | Mẫu gần đây trong bộ nhớ (mặc định 15 phút), cho biểu đồ trực tiếp. Gửi lại `cursor` nhận được để chỉ lấy mẫu mới |
| PUT | `/api/devices/{id}/config` | `{"presence_model"?: P, "presence_threshold"?: P, "prefer_threshold"?: bool, "reset_model_params"?: bool}`, với P = `{"enter", "exit", "confirm_occupied_ms", "confirm_empty_ms"}` | Đổi tham số giữ trạng thái. Backend tăng `rev` và gửi `desired/config` (retained). `reset_model_params` quay về tham số đề xuất của gói |
| POST | `/api/devices/{id}/command` | `{"cmd": "reboot" \| "radar_reinit" \| "ping" \| "republish"}` | Gửi lệnh một lần |
| POST | `/api/devices/{id}/rollback` | | Phát hành lại mô hình "trước" của thiết bị. Mô hình đó phải còn ở trạng thái **đã duyệt**, nếu không trả 409 |
| GET | `/api/rooms` · POST `/api/rooms` | `{"room_id": "[A-Za-z0-9_-]{1,32}", "name"}` | Phòng |

### Phiên thu và nhãn
| Phương thức | Đường dẫn | Thân yêu cầu / tham số | Mô tả |
|---|---|---|---|
| GET | `/api/sessions` | `?device_id=` | Danh sách phiên |
| POST | `/api/sessions` | `{"device_id", "room_id"?, "notes"}` | Bắt đầu phiên thu. Mỗi thiết bị chỉ một phiên đang thu (409 nếu đã có) |
| GET | `/api/sessions/{sid}` | | Phiên, nhãn, cấu hình radar lúc bắt đầu |
| PATCH | `/api/sessions/{sid}` | `{"notes"?, "split"?: "auto"\|"train"\|"val"\|"test", "room_id"?}` | Chỉ đổi trường được gửi. `split` đặt cố định tập, hoặc `auto` để chia tự động |
| POST | `/api/sessions/{sid}/stop` | | Dừng phiên, đóng khoảng nhãn đang mở |
| DELETE | `/api/sessions/{sid}` | | Xóa phiên, nhãn và file mẫu |
| GET | `/api/sessions/{sid}/samples` | `?step=1` | Mẫu của phiên (lấy 1 trên `step` mẫu) cho trình gán nhãn |
| POST | `/api/sessions/{sid}/label-now` | `{"label", "note"}` | Đóng khoảng nhãn hiện tại và mở khoảng mới từ bây giờ. `label` là một trong 4 lớp hoặc `unlabeled` |
| POST | `/api/sessions/{sid}/labels` | `{"start_ms", "end_ms", "label", "note"}` | Gán nhãn cho khoảng `[start_ms, end_ms)`. Phần chồng lên nhãn cũ bị cắt bỏ. `unlabeled` xóa nhãn trong khoảng |
| DELETE | `/api/labels/{label_id}` | | Xóa một khoảng nhãn |
| GET | `/api/events` | `?device_id=` | Sự kiện: báo giả, bỏ sót, sự kiện thiết bị |
| POST | `/api/events` | `{"device_id", "kind": "false_alarm"\|"missed_detection"\|"note", "ts_ms"?, "note"}` | Đánh dấu. Nếu không có phiên đang thu, backend trích 10 phút gần nhất thành phiên `event` chưa gán nhãn |

### Học phòng trống (nền)
| Phương thức | Đường dẫn | Thân yêu cầu | Mô tả |
|---|---|---|---|
| GET | `/api/baselines` | `?device_id=` | Danh sách phiên nền, thống kê, ngưỡng tính ra, cảnh báo |
| POST | `/api/baselines` | `{"device_id", "name", "condition", "disturbance", "duration_s", "confirm_empty": true}` | Bắt đầu. `confirm_empty` phải là `true`: người dùng xác nhận phòng **không có người** |
| POST | `/api/baselines/{id}/finish` | | Kết thúc sớm. Bình thường phiên tự kết thúc sau `duration_s` |
| POST | `/api/baselines/{id}/activate` | `{"active": true, "acknowledge_warnings": false}` | Dùng nền này cho phương pháp ngưỡng (gửi `desired/config`). Nền có trạng thái `review` cần `acknowledge_warnings: true`. `active: false` quay về ngưỡng xuất xưởng |
| DELETE | `/api/baselines/{id}` | | Xóa. Nếu đó là nền đang dùng, thiết bị quay về ngưỡng xuất xưởng |

### Huấn luyện
| Phương thức | Đường dẫn | Thân yêu cầu | Mô tả |
|---|---|---|---|
| GET | `/api/jobs` | | 50 tác vụ gần nhất |
| POST | `/api/jobs` | `{"include_simulated": false, "overrides": {"data": {"split_mode": "session"\|"day"}, "model": {"epochs": 300, "patience": 25}}}` | Chạy huấn luyện nền (một tác vụ tại một thời điểm, 409 nếu đang chạy). `overrides` đè lên [`default.toml`](../training/configs/default.toml) |
| GET | `/api/jobs/{id}` | | Trạng thái `queued`/`running`/`done`/`failed`, tiến độ (`progress.stage`, `epoch`, `val_loss`), lỗi, `model_id` |
| GET | `/api/jobs/{id}/log` | `?lines=200` | Cuối log (text) |
| POST | `/api/jobs/{id}/cancel` | | Dừng tiến trình huấn luyện |

### Mô hình và phát hành
| Phương thức | Đường dẫn | Thân yêu cầu / tham số | Mô tả |
|---|---|---|---|
| GET | `/api/models` | | Danh sách: phiên bản, trạng thái (`evaluated`/`approved`/`rejected`), có dùng dữ liệu mô phỏng không, có đủ dữ liệu kết luận không, tóm tắt đánh giá |
| GET | `/api/models/{id}` | | Chi tiết, kèm `summary` (toàn bộ `report.json`) |
| GET | `/api/models/{id}/report.md` | | Báo cáo đánh giá (Markdown) |
| GET | `/api/models/compare` | `?a=<id>&b=<id>` | So sánh hai mô hình: đánh giá, dữ liệu, cách chia tập |
| POST | `/api/models/{id}/approve` | `{"note", "acknowledge_insufficient": false}` | Duyệt. Cần `report.json`. Mô hình dùng dữ liệu mô phỏng hoặc chưa đủ dữ liệu kết luận cần `acknowledge_insufficient: true` |
| POST | `/api/models/{id}/reject` | `{"note"}` | Từ chối |
| GET | `/api/releases` | `?device_id=` | Bản phát hành: `pending` → `download` → `verify` → `active`/`failed`/`superseded` |
| POST | `/api/releases` | `{"model_id", "device_id"}` | Phát hành mô hình **đã duyệt** cho một thiết bị (gửi `desired/model` retained) |
| POST | `/api/datasets/import` | `{"path": "<đường dẫn trên máy chủ>"}` | Nhập bộ dữ liệu (ví dụ mô phỏng) vào backend |

### Phía thiết bị
| Phương thức | Đường dẫn | Mô tả |
|---|---|---|
| GET | `/api/device/models/{id}/package` | Tải `model.pkg`. Cần token thiết bị. Chỉ mô hình **đã duyệt** (khác thì 403). Header `X-Package-SHA256` |

## Ví dụ (PowerShell)

```powershell
$h = @{ Authorization = "Bearer $env:ADMIN_TOKEN" }
Invoke-RestMethod http://localhost:8000/api/devices -Headers $h
# Bắt đầu phiên thu, gán nhãn "có người ngồi yên" từ bây giờ, rồi dừng
$s = Invoke-RestMethod http://localhost:8000/api/sessions -Method Post -Headers $h -ContentType application/json `
      -Body '{"device_id":"s3-a1b2c3","room_id":"phong-khach","notes":"quạt trần số 2"}'
Invoke-RestMethod "http://localhost:8000/api/sessions/$($s.session_id)/label-now" -Method Post -Headers $h `
      -ContentType application/json -Body '{"label":"occupied_still","note":""}'
Invoke-RestMethod "http://localhost:8000/api/sessions/$($s.session_id)/stop" -Method Post -Headers $h
```

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/api/models
curl -H "Authorization: Bearer $DEVICE_TOKEN" -o model.pkg http://localhost:8000/api/device/models/<id>/package
```
