# Cập nhật mô hình qua mạng

Đây là cơ chế cập nhật **mô hình**, tách biệt với OTA firmware. Firmware không có OTA: bảng phân vùng chỉ có `factory`, và mô hình nằm trong hai phân vùng dữ liệu riêng.

## Gói mô hình `model.pkg` (định dạng v1)

Python tạo gói bằng [`package.py`](../training/presence_ml/package.py); firmware đọc bằng [`model_package.cpp`](../firmware/components/presence/core/model_package.cpp). Mọi số đều little-endian.

| Offset | Trường |
|---|---|
| 0 | `"PSAIPKG1"` |
| 8 | kích thước header (64), số section (≤ 8) |
| 16 | `total_size`: phần được băm và ký |
| 20 | `model_version` (tăng dần, backend cấp) |
| 24 | `fw_api_min`, `fw_api_max`: dải "model API" firmware chấp nhận (firmware này: 1) |
| 28 | `model_id` (32 byte) |
| 60 | cờ (bit 0: huấn luyện bằng dữ liệu mô phỏng) |
| 64 | bảng section, 48 byte mỗi section: loại, offset (căn 16), độ dài, SHA-256 |
| … | `1` CONTRACT (888 byte), `2` TFLITE, `3` TESTVEC, `4` MANIFEST (JSON, chỉ để đọc) |
| `total_size` | phần ký: `"PSG1"`, thuật toán (0 không ký, 1 ECDSA P-256/SHA-256), độ dài, chữ ký DER của SHA-256([0, total_size)) |

- **CONTRACT** là hợp đồng đầu vào ở dạng nhị phân. Gồm: phiên bản hợp đồng, bộ đặc trưng, số đặc trưng (72), chu kỳ mẫu (200 ms), cửa sổ (20), hop (5), quy tắc thiếu dữ liệu, độ phân giải gate (cm), lớp và mặt nạ lớp có người, arena, thang/điểm 0 lượng tử vào/ra, tham số giữ trạng thái đề xuất, bảng hiệu chuẩn, tên lớp, mean/std của 72 đặc trưng. Bố cục chi tiết nằm trong [`contract.py`](../training/presence_ml/contract.py).
- **TESTVEC** chứa các cửa sổ thật (20 × 18 byte, có cả ô thiếu) kèm kết quả máy tính tính được: 72 đặc trưng, 72 int8 đầu vào, 4 int8 đầu ra, cùng sai số cho phép (mặc định ±1 với đầu vào, ±3 với đầu ra, 1e-4 với đặc trưng).
- **MANIFEST** (JSON) là bản cho người đọc: phiên bản, hợp đồng, thứ tự đặc trưng, lớp, lượng tử hóa, SHA-256 từng section, tóm tắt đánh giá. Firmware không tin MANIFEST, chỉ tin CONTRACT và chữ ký.

## Vùng A/B và metadata an toàn khi mất điện

| Phân vùng | Kích thước | Nội dung |
|---|---|---|
| `model_meta` | 8 KB | 2 bản sao (mỗi bản 4 KB) của bản ghi 172 byte: thế hệ, vùng đang dùng, vùng trước, trạng thái từng vùng (`empty`/`writing`/`ready`/`bad`), phiên bản, kích thước, SHA-256, `model_id`, CRC-32 |
| `model_a`, `model_b` | 384 KB mỗi vùng | Gói mô hình nguyên vẹn |

Mỗi thay đổi metadata ghi vào bản sao **không** chứa bản ghi hiện tại, với thế hệ + 1. Khi đọc lại, bản ghi hợp lệ (CRC đúng) có thế hệ cao nhất được chọn. Mất điện giữa chừng thì bản mới hỏng CRC và bản cũ (trạng thái trước) vẫn còn nguyên. Test trên máy tính đã cắt điện ở 793 thời điểm khác nhau của một lần cập nhật; sau khi khởi động lại, mọi lần đều chạy được một mô hình đã kiểm chứng (bản cũ, hoặc bản mới nếu bước kích hoạt đã ghi xong).

Quy trình trên thiết bị ([`model_update.cpp`](../firmware/main/model_update.cpp), [`model_manager.cpp`](../firmware/components/presence/core/model_manager.cpp)):

1. Nhận `desired/model` (retained) với `model_id`, `model_version`, `size`, `sha256`, `release_id`. URL tải là `server_url` cấu hình trên thiết bị + `/api/device/models/<id>/package`; firmware **không** dùng URL nào lấy từ MQTT. Gói đang chạy có cùng phiên bản và SHA-256 thì không làm gì. Gói trùng với vùng "trước" (khôi phục) thì không cần tải lại.
2. Ghi metadata vùng không hoạt động là `writing`, xóa vùng đó, rồi tải HTTP(S) kèm `Authorization: Bearer <token>`. Dữ liệu ghi thẳng vào flash, SHA-256 tính trong lúc tải. **Mô hình đang chạy vẫn hoạt động suốt quá trình này.**
3. Đọc lại gói từ flash vào RAM rồi kiểm tra, theo thứ tự:
   - SHA-256 (khớp metadata và thông báo);
   - header và section, SHA-256 từng section;
   - `fw_api`;
   - chữ ký ECDSA bằng khóa công khai nhúng trong firmware;
   - phiên bản/mã khớp thông báo;
   - hợp đồng: phiên bản, hình dạng, **độ phân giải gate khớp radar**, giá trị hợp lệ;
   - bộ nhớ: còn đủ heap sau khi trừ dự trữ 48 KB.
4. Nạp vào **engine thứ hai** (TFLM): kiểm tra flatbuffer, schema, op, `AllocateTensors`, I/O int8 [1,72] → [1,4], thang lượng tử khớp hợp đồng. Sau đó **chạy thử** mọi vector kiểm tra:
   - đặc trưng tính trên thiết bị so với máy tính;
   - lượng tử hóa so với máy tính;
   - suy luận so với máy tính, trong sai số cho phép.

   Đạt thì chỉ giữ flatbuffer và arena đúng cỡ (khoảng 12,6 KB heap cho mô hình 72-32-16-4), rồi chạy thử lần nữa với cấu hình cuối.
5. Đạt toàn bộ: **một lần ghi metadata** đánh dấu vùng mới `ready` + đang dùng, vùng cũ thành "trước". Sau đó chuyển engine dưới khóa: bước quyết định sau dùng mô hình mới.
6. Bất kỳ bước nào lỗi: vùng mới thành `bad`, mô hình cũ vẫn chạy. Thiết bị gửi `event` với `phase: failed` và lý do, ví dụ:
   - `download: tls -0x2700 x509 0x8 https://…`: chứng chỉ máy chủ không do CA tin cậy ký;
   - `download: HTTP 401`;
   - `sha256: …`;
   - `selftest: …`.

   Thiết bị chỉ thử lại khi nhận lại thông báo `desired/model` (kết nối MQTT lại, hoặc backend phát hành lại). Với cùng phiên bản và SHA-256 đã lỗi: cách nhau ít nhất 10 phút, tối đa 3 lần cho tới lần khởi động sau.
7. Khi khởi động, vùng đang dùng được kiểm tra lại toàn bộ như bước 3–4. Lỗi thì đánh dấu `bad` và chuyển sang vùng trước; vùng trước cũng lỗi thì dùng phương pháp ngưỡng. Thiết bị báo `rolled_back` trong trạng thái.

Trạng thái thiết bị (`status.model`) báo mô hình đang chạy, mô hình trước, trạng thái hai vùng và bước cập nhật gần nhất (`phase`, `error`). Backend cập nhật bảng phát hành theo sự kiện của thiết bị.

## Phát hành và khôi phục (backend)

- Mô hình chỉ được **duyệt** khi đã có báo cáo đánh giá. Mô hình dùng dữ liệu mô phỏng, hoặc báo cáo ghi chưa đủ dữ liệu, phải xác nhận thêm khi duyệt.
- Chỉ mô hình **đã duyệt** mới phát hành được (API `/api/releases`) và tải được (`/api/device/models/<id>/package`, cần token thiết bị).
- Phát hành nghĩa là backend ghi `desired/model` (retained), nên thiết bị đang mất kết nối vẫn nhận được khi kết nối lại.
- **Khôi phục** (`/api/devices/<id>/rollback`) là phát hành lại mô hình "trước" mà thiết bị đang giữ. Thiết bị chuyển vùng không cần tải, nhưng vẫn kiểm tra và chạy thử lại.

## Khóa ký

```powershell
python tools\keys.py init     # secrets\model_signing_key.pem (riêng) + firmware\keys\model_signing_pub.pem (công khai)
```

- Khóa riêng chỉ nằm trên máy chạy backend/huấn luyện (`MODEL_SIGNING_KEY` trong `.env`). Mất khóa thì phải tạo khóa mới và **build lại firmware**.
- `CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE=y` (mặc định): thiếu file khóa công khai thì build dừng với hướng dẫn. Chỉ tắt tùy chọn này khi thử trong LAN; khi đó gói chỉ được kiểm bằng SHA-256 trong thông báo MQTT.
