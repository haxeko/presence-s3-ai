set(embed "")
if(CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE AND NOT CMAKE_BUILD_EARLY_EXPANSION)
    set(key ${CMAKE_CURRENT_LIST_DIR}/../keys/model_signing_pub.pem)
    if(NOT EXISTS ${key})
        message(FATAL_ERROR "Missing firmware/keys/model_signing_pub.pem: run `python tools/keys.py init` "
            "(creates the signing key pair), or disable PRESENCE_MODEL_REQUIRE_SIGNATURE for LAN tests.")
    endif()
    list(APPEND embed ${key})
endif()

# Optional private CA for mqtts:// and https:// (replaces the public CA bundle). Run `idf.py reconfigure` after
# adding or removing the file.
set(private_ca ${CMAKE_CURRENT_LIST_DIR}/../certs/ca.pem)
set(have_private_ca 0)
if(EXISTS ${private_ca} AND NOT CMAKE_BUILD_EARLY_EXPANSION)
    list(APPEND embed ${private_ca})
    set(have_private_ca 1)
endif()

idf_component_register(
    SRCS app_main.cpp console.cpp decision.cpp model_update.cpp net.cpp radar.cpp settings.cpp status.cpp
    INCLUDE_DIRS .
    PRIV_REQUIRES presence nvs_flash esp_driver_uart esp_driver_gpio esp_timer esp_wifi esp_netif esp_eth
        esp_http_client mqtt json console esp_app_format mbedtls
    EMBED_TXTFILES ${embed})

if(have_private_ca)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PRESENCE_PRIVATE_CA=1)
    message(STATUS "TLS: private CA ${private_ca}")
endif()
