# Pipeline huấn luyện

Chạy từ dashboard (tab Huấn luyện) hoặc bằng dòng lệnh. Toàn bộ chạy trên CPU; với vài giờ dữ liệu, một lần huấn luyện mất khoảng chục giây đến vài phút.

```powershell
# Backend tự tạo dataset.json cho mỗi tác vụ (data\jobs\<id>\dataset.json); chạy tay:
.\.venv\Scripts\python.exe -m presence_ml.cli validate --dataset data\jobs\1\dataset.json
.\.venv\Scripts\python.exe -m presence_ml.cli train --dataset data\jobs\1\dataset.json --out data\models\thu-1 --key secrets\model_signing_key.pem
.\.venv\Scripts\python.exe -m presence_ml.cli verify --package data\models\thu-1\model.pkg --pub firmware\keys\model_signing_pub.pem
# Kiểm tra pipeline bằng dữ liệu MÔ PHỎNG:
.\.venv\Scripts\python.exe -m presence_ml.cli simulate --out data\sim --minutes 120
.\.venv\Scripts\python.exe -m presence_ml.cli train --dataset data\sim --out data\models\sim --allow-simulated
```

Cấu hình: [`training/configs/default.toml`](../training/configs/default.toml). Muốn đổi thì chép file, sửa, rồi truyền bằng `--config`. Dashboard cho đổi kiểu chia tập, số epoch và patience.

## Các bước

1. **Kiểm tra dữ liệu** (`dataio.py`), cho từng phiên:
   - tốc độ mẫu thực tế (cảnh báo nếu lệch quá 10% so với 5 Hz), tỉ lệ ô thiếu;
   - số khung radar mỗi mẫu (ước lượng tốc độ radar);
   - mất mẫu trên đường truyền (khoảng trống `seq`), tick trùng, năng lượng ngoài 0–100;
   - tỉ lệ có nhãn, nhãn mâu thuẫn, thời lượng từng lớp.
2. **Dữ liệu không đều / thiếu.** Ô thời gian xác định theo `tick` (200 ms) trong một lần khởi động (`boot_id`). Không nội suy và không lặp mẫu. Tick thiếu là ô thiếu; tick trùng thì giữ mẫu đầu. Không có cửa sổ nào nối qua hai lần khởi động.
3. **Chia train/validation/test theo phiên hoặc theo ngày, TRƯỚC khi tạo cửa sổ** (`split.py`). Mỗi phiên hoặc ngày nằm trọn trong một tập; phiên có tập đặt sẵn trên dashboard được giữ nguyên. Thuật toán thử 400 hoán vị có seed để mỗi tập có đủ lớp và gần tỉ lệ 70/15/15 theo thời lượng có nhãn. Có kiểm tra cuối: không phiên nào có cửa sổ ở hai tập.
4. **Cửa sổ 4 s (20 ô), bước 1 s** (`windows.py`), kết thúc ở tick có `(tick + 1) % 5 == 0`, giống thiết bị. Cửa sổ bị loại (có đếm lý do trong báo cáo) khi:
   - thiếu dữ liệu (< 16/20 ô có mẫu, hoặc thiếu quá 3 ô liên tiếp);
   - có ô chưa gán nhãn hoặc nhãn mâu thuẫn;
   - chứa hai nhãn;
   - nằm gần điểm chuyển nhãn (< 2 s).

   Không nhân bản mẫu hay cửa sổ để cân bằng lớp; mất cân bằng được xử lý bằng class weight "balanced".
5. **72 đặc trưng** (`contract.py`, xem [data-format.md](data-format.md)), float32. Kết quả giống từng bit giữa Python, C++ trên máy tính và ESP32-S3 (đã kiểm chứng trên QEMU).
6. **Chuẩn hóa** z-score: mean và std chỉ học từ tập train, std được chặn dưới ở 0,5.
7. **Mô hình** Keras 72 → Dense 32 ReLU → Dense 16 ReLU → Dense 4 Softmax, Adam (lr 0,001). Early stopping theo `val_loss` (patience 25), trọng số tốt nhất được lưu (`model_best.keras`).
8. **Lượng tử hóa full INT8** (đầu vào và đầu ra int8) bằng `TFLiteConverter`. Dữ liệu hiệu chuẩn là 1000 cửa sổ chọn ngẫu nhiên (seed) **từ tập train**. Mô hình INT8 được chạy lại bằng kernel tham chiếu của TFLite để so với mô hình float.
9. **Đánh giá** (`evaluate.py`): phát lại chuỗi thời gian của tập test qua toàn bộ chuỗi quyết định (cửa sổ → điểm → logic giữ trạng thái), giống thiết bị, với bốn phương pháp:
   - mô hình INT8;
   - mô hình float;
   - ngưỡng xuất xưởng LD2410;
   - ngưỡng theo phiên nền đang dùng (nếu có).
10. **Hiệu chuẩn điểm**: hồi quy isotonic trên validation, tạo bảng tối đa 16 điểm nạp kèm gói. ECE được đo trên test trước và sau hiệu chuẩn. Validation có dưới 50 cửa sổ hoặc chỉ có một loại nhãn thì không tạo bảng.
11. **Gói mô hình** (`package.py`): `model.tflite`, hợp đồng nhị phân, 24 vector kiểm tra từ tập test, manifest JSON, chữ ký ECDSA P-256. Kết quả là `model.pkg`, cùng `report.json`, `report.md` và `replay_test.csv`.

## Đọc báo cáo

- **Theo dòng thời gian** là phần quan trọng nhất: kết quả sau logic giữ trạng thái, giống thứ người dùng thấy.
  - *báo giả*: mỗi đoạn liên tục quyết định "có người" trong lúc nhãn là trống; báo cáo ghi số lần và số lần mỗi giờ phòng trống;
  - *bỏ sót*: thời gian có người nhưng quyết định là trống, và khoảng bỏ sót dài nhất;
  - *độ trễ vào phòng*: từ lúc nhãn đổi sang có người tới lúc quyết định "có người" (giới hạn 60 s);
  - *độ trễ ra*: tới lúc quyết định "trống" (giới hạn 600 s). Độ trễ ra phụ thuộc chủ yếu vào `confirm_empty_ms`.
- **Theo cửa sổ**: confusion matrix 4 lớp, precision/recall/F1 từng lớp, và kết quả nhị phân sau khi gộp lớp. Có cho cả validation và test, cả float và INT8.
- **Trước/sau lượng tử hóa**: tỉ lệ argmax trùng, độ chính xác, sai lệch điểm lớn nhất.
- **Dữ liệu**: chất lượng từng phiên, cách chia tập, số cửa sổ bị loại theo lý do.
- **"CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN"** xuất hiện khi tập test có < 5 phút cho một lớp, một lớp chỉ có ở < 2 phiên/ngày, có < 0,5 giờ phòng trống, hoặc dữ liệu là mô phỏng. Các ngưỡng này nằm trong mục `[evaluation]` của cấu hình. Khi đó vẫn có thể duyệt mô hình để **thử nghiệm**, nhưng phải xác nhận thêm.

Một lần huấn luyện **không mặc định tốt hơn** mô hình đang chạy. Hãy so sánh các mô hình (tab Mô hình, chọn 2, "So sánh"), chú ý chúng có cùng tập test hay không, rồi mới duyệt và phát hành.

## Kiểm tra nhất quán thiết bị ↔ máy tính

Sau khi thiết bị chạy mô hình X một thời gian, chạy lệnh sau:

```powershell
.\.venv\Scripts\python.exe -m presence_ml.cli consistency --dataset data\sessions\<phiên>\samples.csv --package data\models\<X>\model.pkg
```

Phiên dùng để so phải được thu (tab Thu dữ liệu) trong lúc thiết bị chạy mô hình X; không cần gán nhãn.

Lệnh dựng lại đúng các cửa sổ thiết bị đã chấm điểm, chạy lại bằng Python, rồi so đầu ra int8 (`q0..q3`) với giá trị thiết bị đã gửi, trong ngưỡng sai số của gói. Hai loại cửa sổ bị bỏ qua:
- cửa sổ có mẫu bị mất trên đường truyền;
- cửa sổ bắt đầu trước mẫu đầu tiên của phiên.

Lệch nhiều là dấu hiệu hai bên xử lý đầu vào khác nhau.

Kết quả với firmware thật chạy trong QEMU: 131 cửa sổ, 0 lệch, sai khác lớn nhất 2 LSB ([verification.md](verification.md)).
