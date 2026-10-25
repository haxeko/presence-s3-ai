// Host implementation of core/crypto.h with OpenSSL (the ESP32 uses mbedTLS: platform/crypto_mbedtls.cpp).
#include "crypto.h"

#include <openssl/bio.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/sha.h>
#include <stdio.h>
#include <string.h>

namespace presence {

Sha256::Sha256()
{
    SHA256_CTX *c = new SHA256_CTX;
    SHA256_Init(c);
    ctx_ = c;
}

Sha256::~Sha256()
{
    delete (SHA256_CTX *)ctx_;
}

void Sha256::update(const void *data, size_t len)
{
    SHA256_Update((SHA256_CTX *)ctx_, data, len);
}

void Sha256::finish(uint8_t out[32])
{
    SHA256_Final(out, (SHA256_CTX *)ctx_);
}

void sha256(const void *data, size_t len, uint8_t out[32])
{
    SHA256((const unsigned char *)data, len, out);
}

bool ecdsa_p256_verify(const char *public_key_pem, const uint8_t digest[32], const uint8_t *sig, size_t sig_len)
{
    BIO *bio = BIO_new_mem_buf(public_key_pem, -1);
    EVP_PKEY *key = bio ? PEM_read_bio_PUBKEY(bio, nullptr, nullptr, nullptr) : nullptr;
    BIO_free(bio);
    if (!key) {
        return false;
    }
    bool ok = false;
    EVP_PKEY_CTX *ctx = EVP_PKEY_CTX_new(key, nullptr);
    if (ctx && EVP_PKEY_verify_init(ctx) == 1 && EVP_PKEY_CTX_set_signature_md(ctx, EVP_sha256()) == 1) {
        ok = EVP_PKEY_verify(ctx, sig, sig_len, digest, 32) == 1;
    }
    EVP_PKEY_CTX_free(ctx);
    EVP_PKEY_free(key);
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
