"""Mô hình Keras: 72 -> Dense 32 ReLU -> Dense 16 ReLU -> Dense 4 Softmax (cấu hình được)."""

from __future__ import annotations

import numpy as np

from . import contract as C


def build_model(hidden: list[int], learning_rate: float, seed: int):
    import keras

    keras.utils.set_random_seed(seed)
    inp = keras.Input(shape=(C.N_FEATURES,), name="features", dtype="float32")
    x = inp
    for i, h in enumerate(hidden):
        x = keras.layers.Dense(int(h), activation="relu", name=f"dense_{i}")(x)
    out = keras.layers.Dense(C.N_CLASSES, activation="softmax", name="classes")(x)
    model = keras.Model(inp, out, name="presence_mlp")
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=learning_rate),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )
    return model


def class_weights(labels: np.ndarray, mode: str) -> dict[int, float] | None:
    if mode == "none":
        return None
    counts = np.bincount(labels, minlength=C.N_CLASSES).astype(float)
    present = counts > 0
    w = np.zeros(C.N_CLASSES)
    w[present] = counts.sum() / (present.sum() * counts[present])
    return {int(k): float(w[k]) for k in range(C.N_CLASSES) if present[k]}
