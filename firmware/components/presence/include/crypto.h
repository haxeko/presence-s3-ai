// Hash and signature primitives used by the model package checks. Implemented with mbedTLS on the ESP32
// (platform/crypto_mbedtls.cpp) and with OpenSSL in the host tests (test/host/crypto_openssl.cpp).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace presence {

class Sha256 {
public:
    Sha256();
    ~Sha256();
    void update(const void *data, size_t len);
    void finish(uint8_t out[32]);

private:
    void *ctx_;
};

void sha256(const void *data, size_t len, uint8_t out[32]);

// ECDSA P-256 signature (DER) of a SHA-256 digest, checked with a PEM public key.
bool ecdsa_p256_verify(const char *public_key_pem, const uint8_t digest[32], const uint8_t *sig, size_t sig_len);

void hex_encode(const uint8_t *data, size_t len, char *out); // out: 2 * len + 1 chars
bool hex_decode(const char *hex, uint8_t *out, size_t len);  // exactly 2 * len hex digits

} // namespace presence
