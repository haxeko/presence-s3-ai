# Presence core (pure C++, also built by the host tests in firmware/test/host) + ESP32 implementations of its
# interfaces (mbedTLS crypto, TensorFlow Lite Micro engine, partition access).
idf_component_register(
    SRCS
        core/ld2410_parser.cpp core/ld2410_protocol.cpp core/model_contract.cpp core/model_manager.cpp
        core/model_package.cpp core/model_selftest.cpp core/model_store.cpp core/pipeline.cpp
        core/presence_logic.cpp core/sample_codec.cpp core/sample_ring.cpp core/sampler.cpp
        core/threshold_detector.cpp core/window_features.cpp
        esp/crypto_mbedtls.cpp esp/partition_region.cpp esp/tflm_engine.cpp
    INCLUDE_DIRS include
    REQUIRES esp_partition
    PRIV_REQUIRES mbedtls esp-tflite-micro heap)
# Bit-exact features: no fast-math anywhere in this component
target_compile_options(${COMPONENT_LIB} PRIVATE -fno-fast-math -ffp-contract=off)
