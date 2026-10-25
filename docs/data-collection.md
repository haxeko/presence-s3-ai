# Thu dữ liệu và gán nhãn

## Bốn lớp và trạng thái chưa gán nhãn

| Nhãn | Nghĩa |
|---|---|
| `empty_quiet` | Phòng trống, ít nhiễu |
| `empty_disturbance` | Phòng trống, có quạt, rèm, máy lạnh hay nguồn nhiễu quen thuộc |
| `occupied_still` | Có người ngồi hoặc nằm yên (đọc sách, làm việc, ngủ) |
| `occupied_moving` | Có người đi lại hoặc cử động nhiều |
| *(chưa gán nhãn)* | Không biết: **không** được coi là phòng trống, không dùng để huấn luyện và không tính vào đánh giá |

Nhãn chỉ do **người xác nhận** (`source = human`). Kết quả của thiết bị (quyết định, lớp dự đoán, điểm) được lưu riêng trong từng mẫu (cột `p_decision`, `pred`, `q0..q3`, `occ_pm`). Dashboard hiển thị hai thứ này trên hai hàng khác nhau, không lẫn với nhau.

## Một phiên thu

1. Tab **Thu dữ liệu**: chọn thiết bị và phòng, ghi chú môi trường. Ví dụ: "quạt trần gate 3 chạy số 2, rèm cửa sổ gate 5, ghế sofa gate 2". Bấm **Bắt đầu phiên**. Phiên tự lưu cấu hình radar thiết bị đang báo (phiên bản, độ phân giải gate, ngưỡng, gate xa nhất).
2. Mỗi khi tình trạng phòng thay đổi, bấm nút nhãn tương ứng ngay. Nút đó đóng khoảng nhãn trước và mở khoảng mới. "Chưa gán nhãn" chỉ đóng khoảng nhãn hiện tại.
3. Bấm **Dừng phiên** khi xong.
4. Tab **Phiên & nhãn**: mở phiên, kéo chuột trên biểu đồ để chọn một khoảng, rồi gán hoặc xóa nhãn. Khoảng mới đè lên khoảng cũ, nên không bao giờ có hai nhãn chồng nhau. Có thể đặt cố định phiên vào tập train, val hoặc test (mặc định "tự động").

Khi huấn luyện, cửa sổ chứa thời điểm đổi nhãn và các ô cách điểm đổi nhãn dưới 2 s (`transition_guard_s`) bị loại tự động. Vì vậy chỉ cần bấm nhãn đúng lúc, không cần chính xác tới từng giây.

## Đánh dấu báo giả / bỏ sót

Ở tab **Tổng quan** có hai nút:
- **Đánh dấu báo giả**: thiết bị báo có người nhưng phòng trống.
- **Đánh dấu bỏ sót**: có người nhưng thiết bị báo trống.

Mỗi lần bấm, backend lưu một sự kiện (thời điểm và ghi chú). Nếu lúc đó không có phiên đang thu, backend trích 10 phút dữ liệu gần nhất trong bộ nhớ thành một phiên loại `event`, **chưa gán nhãn**. Bạn mở phiên đó và gán nhãn đúng; nó sẽ được dùng cho lần huấn luyện sau. Muốn bỏ các phiên `event` khỏi huấn luyện thì đặt `exclude_kinds = ["event"]` trong cấu hình.

## Tình huống khó nên thu

Thu mỗi tình huống nhiều lần, vào nhiều ngày khác nhau (để chia tập theo ngày), mỗi lần ít nhất vài phút:

| Tình huống | Nhãn |
|---|---|
| Người ngồi yên (đọc sách, xem điện thoại) **khi quạt đang chạy** | `occupied_still` |
| Người ngồi yên **cùng khoảng cách (cùng gate) với quạt** | `occupied_still` |
| Người **ở xa** (gate 6–8), nằm ngủ, thở nhẹ | `occupied_still` |
| Người **ra khỏi phòng nhưng quạt vẫn chạy**, rèm vẫn bay | `empty_disturbance` |
| Phòng trống yên tĩnh, ban ngày và ban đêm | `empty_quiet` |
| Đi lại, vào và ra phòng nhiều lần | `occupied_moving` (giúp đo độ trễ vào/ra) |
| Thú cưng, robot hút bụi (nếu có) | ghi chú môi trường + nhãn trống/có người theo ý muốn của bạn |

Số liệu đánh giá chỉ đáng tin khi tập test có đủ dữ liệu. Mặc định cần ≥ 5 phút mỗi lớp, mỗi lớp xuất hiện ở ≥ 2 phiên/ngày khác nhau, và ≥ 0,5 giờ phòng trống. Thiếu thì báo cáo ghi **"CHƯA ĐỦ DỮ LIỆU ĐỂ KẾT LUẬN"**.

## Học phòng trống

Tab **Học phòng trống**:
1. Nhập tên và điều kiện, ví dụ "quạt bật số 2, đêm". Tích "có nguồn nhiễu" nếu quạt/rèm đang hoạt động.
2. Chọn thời lượng (mặc định 120 s).
3. **Tích "Tôi xác nhận trong phòng KHÔNG có người"** (bắt buộc), bấm Bắt đầu, rồi rời khỏi phòng.

Kết thúc phiên, backend tính thống kê 18 kênh và kiểm tra dấu hiệu có người. Nếu năng lượng tĩnh > 60 trong hơn 30% mẫu, radar tự báo mục tiêu tĩnh, hoặc mô hình trên thiết bị kết luận có người, trạng thái sẽ là **"cần xem lại"**. Nền không tự áp dụng: chỉ khi bấm **Dùng làm nền** (có xác nhận thêm nếu có cảnh báo), ngưỡng tính từ nền mới được gửi xuống làm phương pháp ngưỡng đối chứng. Có thể giữ nhiều phiên nền cho nhiều điều kiện, mỗi thiết bị chỉ dùng một phiên tại một thời điểm.

Phiên nền cũng là một phiên thu có nhãn trống do người xác nhận, nên cũng được dùng khi huấn luyện.

## Không có phần cứng

`tools/fake_device.py` gửi dữ liệu **mô phỏng** theo đúng định dạng của thiết bị (kịch bản `quiet`, `fan`, `still`, `moving`, `mix`) để thử toàn bộ quy trình trên dashboard. Không dùng dữ liệu mô phỏng để kết luận về khả năng phát hiện người thật. Mọi phiên mô phỏng được đánh dấu `source = simulated`, và huấn luyện loại chúng trừ khi chọn "Cho phép dữ liệu mô phỏng".
