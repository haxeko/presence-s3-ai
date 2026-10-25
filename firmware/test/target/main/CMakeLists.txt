idf_component_register(
    SRCS target_main.cpp
    REQUIRES presence nvs_flash esp_timer esp_partition
    EMBED_FILES ../../fixtures/model/model_v1.pkg ../../fixtures/model/model_v2.pkg
    EMBED_TXTFILES ../../fixtures/features.txt ../../fixtures/model/signing_pub.pem)
