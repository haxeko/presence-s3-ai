"""Xuất mô hình full INT8 (TFLite) bằng dữ liệu hiệu chuẩn lấy từ tập train, và chạy lại mô hình INT8.

Đầu vào/đầu ra của mô hình là int8; chuẩn hóa và lượng tử hóa đầu vào làm bên ngoài (contract.py /
model_contract.cpp) để mọi đặc trưng dùng chung một thang lượng tử hợp lý.
Kết quả kỳ vọng cho vector kiểm tra lấy bằng kernel THAM CHIẾU (BUILTIN_REF), gần với TFLM trên ESP32.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from . import contract as C


def _interpreter_class():
    """tf.lite.Interpreter của TensorFlow đã cố định phiên bản (có trên cả Windows; TF 2.21 báo "deprecated"
    nhưng vẫn có). Nếu bản TF sau này bỏ tf.lite thì dùng LiteRT (ai-edge-litert, chưa có bản Windows)."""
    import warnings

    import tensorflow as tf

    if hasattr(tf, "lite") and hasattr(tf.lite, "Interpreter"):
        warnings.filterwarnings("ignore", message=".*tf.lite.Interpreter is deprecated.*")
        return tf.lite.Interpreter, tf.lite.experimental.OpResolverType
    from ai_edge_litert.interpreter import Interpreter, OpResolverType  # type: ignore

    return Interpreter, OpResolverType


def convert_int8(model, representative: np.ndarray) -> bytes:
    import tensorflow as tf

    rep = np.asarray(representative, dtype=np.float32)

    def rep_data():
        for i in range(len(rep)):
            yield [rep[i: i + 1]]

    def configure(conv):
        conv.optimizations = [tf.lite.Optimize.DEFAULT]
        conv.representative_dataset = rep_data
        conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
        conv.inference_input_type = tf.int8
        conv.inference_output_type = tf.int8
        return conv

    try:
        conv = configure(tf.lite.TFLiteConverter.from_keras_model(model))
        return conv.convert()
    except Exception:  # Keras 3 với một số bản TF: đi qua SavedModel
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "saved"
            model.export(str(p), format="tf_saved_model")
            conv = configure(tf.lite.TFLiteConverter.from_saved_model(str(p)))
            return conv.convert()


class Int8Model:
    """Chạy mô hình .tflite INT8 theo lô bằng kernel tham chiếu (một mẫu mỗi lần invoke, giống thiết bị)."""

    def __init__(self, tflite: bytes, reference_kernels: bool = True):
        Interpreter, OpResolverType = _interpreter_class()
        kw = {"model_content": tflite}
        if reference_kernels:
            kw["experimental_op_resolver_type"] = OpResolverType.BUILTIN_REF
        self.it = Interpreter(**kw)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        if self.inp["dtype"] != np.int8 or self.out["dtype"] != np.int8:
            raise ValueError("model I/O must be int8")
        if list(self.inp["shape"]) != [1, C.N_FEATURES] or list(self.out["shape"]) != [1, C.N_CLASSES]:
            raise ValueError(f"unexpected shapes {self.inp['shape']} {self.out['shape']}")
        self.input_scale, self.input_zero_point = self.inp["quantization"]
        self.output_scale, self.output_zero_point = self.out["quantization"]

    def __call__(self, q_in: np.ndarray) -> np.ndarray:
        q_in = np.asarray(q_in, dtype=np.int8).reshape(-1, C.N_FEATURES)
        out = np.zeros((len(q_in), C.N_CLASSES), dtype=np.int8)
        for i in range(len(q_in)):
            self.it.set_tensor(self.inp["index"], q_in[i: i + 1])
            self.it.invoke()
            out[i] = self.it.get_tensor(self.out["index"])[0]
        return out

    def ops(self) -> list[str]:
        try:
            return sorted({d["op_name"] for d in self.it._get_ops_details()})  # noqa: SLF001
        except Exception:
            return []


def tensor_arena_estimate(tflite: bytes) -> int:
    """Ước lượng thô kích thước tensor cần trên TFLM (đo thật trên thiết bị qua self-test)."""
    m = Int8Model(tflite)
    total = 0
    for d in m.it.get_tensor_details():
        total += int(np.prod(d["shape"])) * np.dtype(d["dtype"]).itemsize
    return total
