# Định dạng dữ liệu

## Hợp đồng đầu vào v1 (`energy18_stats4_v1`)

Nguồn chuẩn: [`training/presence_ml/contract.py`](../training/presence_ml/contract.py) và [`contract_v1.h`](../firmware/components/presence/include/contract_v1.h). Mọi thay đổi đều cần phiên bản hợp đồng mới, nâng `kFirmwareModelApi` và huấn luyện lại.

- **18 kênh** theo thứ tự: `m0..m8` (năng lượng chuyển động gate 0–8), rồi `s0..s8` (năng lượng tĩnh). Số nguyên 0–100.
- **Mẫu 5 Hz:** ô 200 ms lấy giá trị **lớn nhất** theo kênh của các khung Engineering đủ 9+9 gate trong ô (chặn ở 100). Ô không có khung hợp lệ là ô thiếu.
- **Cửa sổ:** 20 ô (4 s). Đánh giá ở tick có `(tick + 1) % 5 == 0` (mỗi 1 s). Dùng được khi có ≥ 16 ô có mẫu và không thiếu quá 3 ô liên tiếp.
- **72 đặc trưng float32**, chỉ số = `thống_kê × 18 + kênh`, với thống kê theo thứ tự `mean, std, max, mad`. Tên: `mean_m0 … mean_s8, std_m0 …, max_m0 …, mad_m0 … mad_s8`.

  Với n ô hợp lệ của một kênh, S1 = Σx, S2 = Σx² (số nguyên):
  - `mean = float32(S1) / float32(n)`
  - `std  = sqrtf(float32(n·S2 − S1²) / float32(n²))` (độ lệch chuẩn tổng thể)
  - `max  = float32(max x)`
  - `mad  = float32(D) / float32(P)`, với D = Σ|xᵢ − xᵢ₋₁| trên P cặp ô **kề nhau cùng hợp lệ**; P = 0 thì `mad = 0`

  Mọi tổng là số nguyên, chỉ có một phép chia hoặc căn float32 cuối cùng, nên Python, C++ x86 và ESP32-S3 cho kết quả giống từng bit.
- **Chuẩn hóa:** `z = (f − mean) / std` (float32; mean và std lấy từ tập train, std ≥ 0,5).
- **Lượng tử hóa:** `q = clamp(lroundf(z / input_scale) + input_zero_point, −128, 127)`, làm tròn nửa ra xa 0.
- **Đầu ra:** 4 int8 theo lớp `empty_quiet, empty_disturbance, occupied_still, occupied_moving`. Điểm có người = `((q2 − zp) + (q3 − zp)) × output_scale`.

## Lô mẫu MQTT (schema 1), chủ đề `presence/v1/dev/<id>/samples`

```json
{"v":1,"dev":"s3-a1b2c3","boot":3735928559,"t0":5012,"now":98000,"epoch":1761400000123,"rows":[[...40 số nguyên...], ...]}
```

- `boot`: số ngẫu nhiên mỗi lần khởi động.
- `t0`: uptime (ms) lúc bắt đầu tick 0.
- `now`: uptime lúc gửi.
- `epoch`: giờ thiết bị (ms, 0 nếu chưa có SNTP).

Mẫu của tick k kết thúc ở `t0 + 200(k+1)` (uptime). Backend quy ra giờ thực và ghi kèm `ts_src`:
- `device`: thiết bị đã có SNTP. Giờ = `epoch − (now − thời điểm mẫu)`.
- `server`: thiết bị chưa có SNTP, ví dụ LAN không ra Internet. Giờ = thời điểm mẫu + độ lệch uptime → giờ backend.
  - Mỗi lô cho một giá trị `giờ nhận − now`, bằng độ lệch thật cộng độ trễ. Độ lệch được ước lượng bằng giá trị nhỏ nhất gặp trong lần khởi động đó.
  - Giá trị này chỉ được tăng tối đa 100 ppm theo thời gian thiết bị, để theo trôi của thạch anh.
  - Ước lượng được lưu trong SQLite (`device_clock`), nên lô đến muộn vì xếp hàng ở broker (ví dụ backend vừa khởi động lại) vẫn đúng giờ.

  Đã thử với firmware trong QEMU: backend tắt 60 s giữa phiên thu; sau đó 674 mẫu liền `seq`, bước thời gian 193–201 ms.

| # | Cột | Ý nghĩa |
|---|---|---|
| 0 | `seq` | Số thứ tự mẫu, liền nhau trong một lần khởi động. Khoảng trống = mẫu mất (bộ đệm đầy hoặc đường truyền) |
| 1 | `tick` | Ô 200 ms từ lúc khởi động. Khoảng trống tick không đi kèm khoảng trống seq = radar không gửi khung hợp lệ |
| 2 | `nf` | Số khung radar hợp lệ trong ô |
| 3 | `rs` | Trạng thái mục tiêu của radar (0 không, 1 chuyển động, 2 tĩnh, 3 cả hai; 4–6 khi radar dò nhiễu nền) |
| 4–8 | `mv_cm, mv_e, st_cm, st_e, det_cm` | Khoảng cách và năng lượng mục tiêu chuyển động/tĩnh, khoảng cách phát hiện (của khung cuối trong ô) |
| 9–26 | `m0..m8, s0..s8` | Năng lượng theo gate |
| 27–28 | `light, out` | Byte phụ của LD2410B/C (ánh sáng, chân OUT), −1 khi radar không gửi |
| 29 | `ev` | 1 nếu thiết bị chấm điểm cửa sổ kết thúc ở tick này |
| 30 | `meth` | 0 chưa có, 1 ngưỡng xuất xưởng, 2 ngưỡng theo nền, 3 mô hình |
| 31 | `mver` | Phiên bản mô hình (0 khi không dùng mô hình) |
| 32 | `pred` | Lớp argmax (−1 khi không có) |
| 33–36 | `q0..q3` | Đầu ra int8 của mô hình |
| 37 | `occ` | Điểm có người × 1000 (−1 khi cửa sổ thiếu dữ liệu) |
| 38 | `ps` | Trạng thái báo cáo: 0 chưa chắc, 1 trống, 2 có người |
| 39 | `pd` | Quyết định: 0 chưa có, 1 trống, 2 có người |

Các cột 30–39 mang kết quả gần nhất tại thời điểm mẫu, là **dự đoán của thiết bị**, không phải nhãn.

## File của backend

- `data/presence.db` (SQLite): `devices`, `rooms`, `sessions` (thiết bị, phòng, loại `collect`/`baseline`/`event`, nguồn `device`/`simulated`, thời gian, cấu hình radar, ghi chú, tập cố định), `labels` (khoảng `[start_ms, end_ms)`, nhãn, `source = human`), `events` (báo giả, bỏ sót, sự kiện thiết bị), `baselines`, `jobs`, `models`, `releases`.
- `data/sessions/<session_id>/samples.csv`: một dòng mỗi mẫu. Các cột: `session_id, device_id, boot_id, seq, tick, t_dev_ms, ts_ms, ts_src, n_frames, radar_state, mv_cm, mv_e, st_cm, st_e, det_cm, m0..m8, s0..s8, light, out_pin, ev, method, model_version, pred, q0..q3, occ_pm, p_state, p_decision`.
- `data/models/<model_id>/`: `model.pkg`, `model.tflite`, `model_best.keras`, `report.json`, `report.md`, `replay_test.csv`.
- `data/jobs/<job_id>/`: `dataset.json`, `log.txt`.

## `dataset.json` (đầu vào của huấn luyện)

```json
{"info": {...},
 "sessions": [{"session_id": "...", "device_id": "...", "room_id": "...", "kind": "collect", "source": "device",
               "started_at_ms": 0, "ended_at_ms": 0, "samples_path": "đường dẫn tuyệt đối hoặc tương đối tới samples.csv",
               "labels": [{"start_ms": 0, "end_ms": 0, "label": "occupied_still"}], "split": null, "notes": "", "radar": {}}],
 "baselines": [{"baseline_id": 1, "device_id": "...", "active": true, "stats": {"mean": [], "p99": [], "max": []}}]}
```

Nhãn của một ô lấy theo thời điểm của ô. Ô ngoài mọi khoảng là **chưa gán nhãn**; ô nằm trong hai nhãn khác nhau là **mâu thuẫn**. Cả hai loại đều bị loại khi huấn luyện và đánh giá.
