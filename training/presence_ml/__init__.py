"""presence_ml: dữ liệu, đặc trưng, huấn luyện, lượng tử hóa INT8 và đánh giá cho cảm biến hiện diện LD2410.

Các module không phụ thuộc TensorFlow (contract, ld2410, sampler, presence_logic, threshold, dataio,
windows, split, package, sim) được backend và công cụ dùng lại. TensorFlow chỉ được import trong
train/export/evaluate khi thật sự cần.
"""

__version__ = "0.1.0"
