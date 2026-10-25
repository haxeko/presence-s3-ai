// core/crypto.h on the ESP32: mbedTLS (hardware SHA on the ESP32-S3).
#include "crypto.h"

#include <string.h>

#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"

namespace presence {

Sha256::Sha256()
{
    mbedtls_sha256_context *c = new mbedtls_sha256_context;
    mbedtls_sha256_init(c);
    mbedtls_sha256_starts(c, 0);
    ctx_ = c;
}

Sha256::~Sha256()
{
    mbedtls_sha256_context *c = (mbedtls_sha256_context *)ctx_;
    mbedtls_sha256_free(c);
    delete c;
}

void Sha256::update(const void *data, size_t len)
{
    mbedtls_sha256_update((mbedtls_sha256_context *)ctx_, (const unsigned char *)data, len);
}

void Sha256::finish(uint8_t out[32])
{
    mbedtls_sha256_finish((mbedtls_sha256_context *)ctx_, out);
}

void sha256(const void *data, size_t len, uint8_t out[32])
{
    mbedtls_sha256((const unsigned char *)data, len, out, 0);
}

bool ecdsa_p256_verify(const char *public_key_pem, const uint8_t digest[32], const uint8_t *sig, size_t sig_len)
{
    mbedtls_pk_context pk;
    mbedtls_pk_init(&pk);
    bool ok = false;
    // PEM input must include the terminating NUL in its length
    if (mbedtls_pk_parse_public_key(&pk, (const unsigned char *)public_key_pem, strlen(public_key_pem) + 1) == 0 &&
        mbedtls_pk_can_do(&pk, MBEDTLS_PK_ECKEY)) {
        ok = mbedtls_pk_verify(&pk, MBEDTLS_MD_SHA256, digest, 32, sig, sig_len) == 0;
    }
    mbedtls_pk_free(&pk);
    return ok;
}

void hex_encode(const uint8_t *data, size_t len, char *out)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[2 * i] = digits[data[i] >> 4];
        out[2 * i + 1] = digits[data[i] & 0x0F];
    }
    out[2 * len] = '\0';
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

bool hex_decode(const char *hex, uint8_t *out, size_t len)
{
    if (strlen(hex) != 2 * len) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        const int hi = hex_value(hex[2 * i]);
        const int lo = hex_value(hex[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return true;
}

} // namespace presence
