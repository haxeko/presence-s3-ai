# Host tests of the firmware core: plain C++17 + OpenSSL or mbedTLS (SHA-256 / ECDSA).
#   cmake -S firmware/test/host -B build/host-tests && cmake --build build/host-tests && build/host-tests/host_tests
# INT8 inference with TensorFlow Lite Micro is tested on the ESP32-S3 ISA in QEMU (firmware/test/qemu).
cmake_minimum_required(VERSION 3.16)
project(presence_host_tests CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Crypto backend: OpenSSL when installed, else the mbedTLS sources of ESP-IDF ($IDF_PATH, e.g. inside the
# espressif/idf Docker image) with the same crypto_mbedtls.cpp as the ESP32. Force with -DPRESENCE_CRYPTO=mbedtls.
set(PRESENCE_CRYPTO "auto" CACHE STRING "openssl, mbedtls or auto")
if(NOT PRESENCE_CRYPTO STREQUAL "mbedtls")
    find_package(OpenSSL)
endif()

set(CORE ${CMAKE_CURRENT_LIST_DIR}/../../components/presence)
file(GLOB CORE_SOURCES ${CORE}/core/*.cpp)

if(OpenSSL_FOUND)
    set(CRYPTO_SOURCE crypto_openssl.cpp)
elseif(DEFINED ENV{IDF_PATH})
    set(ENABLE_PROGRAMS OFF CACHE BOOL "" FORCE)
    set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
    set(MBEDTLS_FATAL_WARNINGS OFF CACHE BOOL "" FORCE)
    add_subdirectory($ENV{IDF_PATH}/components/mbedtls/mbedtls ${CMAKE_BINARY_DIR}/mbedtls EXCLUDE_FROM_ALL)
    set(CRYPTO_SOURCE ${CORE}/esp/crypto_mbedtls.cpp)
else()
    message(FATAL_ERROR "Need OpenSSL (libssl-dev) or IDF_PATH (mbedTLS sources) for the host tests")
endif()

add_executable(host_tests
    test_main.cpp test_util.cpp test_ld2410.cpp test_features.cpp test_sampling.cpp test_model.cpp
    ${CRYPTO_SOURCE} ${CORE_SOURCES})
target_include_directories(host_tests PRIVATE ${CORE}/include ${CMAKE_CURRENT_LIST_DIR})
target_compile_definitions(host_tests PRIVATE FIXTURE_DIR="${CMAKE_CURRENT_LIST_DIR}/../fixtures"
    OPENSSL_API_COMPAT=0x10100000L OPENSSL_SUPPRESS_DEPRECATED)
# Same warnings as the ESP-IDF build, plus a few more
target_compile_options(host_tests PRIVATE -Wall -Wextra -Werror -Wno-unused-parameter -Wno-missing-field-initializers
    -Wno-sign-compare -Wno-deprecated-declarations)
if(OpenSSL_FOUND)
    target_link_libraries(host_tests PRIVATE OpenSSL::Crypto)
    message(STATUS "host tests: OpenSSL crypto")
else()
    target_link_libraries(host_tests PRIVATE mbedcrypto)
    message(STATUS "host tests: mbedTLS crypto from $ENV{IDF_PATH}")
endif()

enable_testing()
add_test(NAME host_tests COMMAND host_tests)
