#include "model_package.h"

#include <string.h>

#include "crypto.h"

namespace presence {

static uint32_t rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t rd_u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

const PkgSection *PkgInfo::find(uint32_t type) const
{
    for (uint32_t i = 0; i < section_count; i++) {
        if (sections[i].type == type) {
            return &sections[i];
        }
    }
    return nullptr;
}

const char *pkg_error_name(PkgError e)
{
    switch (e) {
    case PkgError::Ok:
        return "ok";
    case PkgError::TooSmall:
        return "package_too_small";
    case PkgError::Magic:
        return "package_magic";
    case PkgError::Header:
        return "package_header";
    case PkgError::Sections:
        return "package_sections";
    case PkgError::SectionHash:
        return "section_hash";
    case PkgError::Signature:
        return "signature";
    case PkgError::Incompatible:
        return "firmware_incompatible";
    }
    return "?";
}

PkgError parse_package(const uint8_t *pkg, size_t len, PkgInfo *out)
{
    if (len < kPkgHeaderSize + 8) {
        return PkgError::TooSmall;
    }
    if (memcmp(pkg, "PSAIPKG1", 8) != 0) {
        return PkgError::Magic;
    }
    PkgInfo info;
    memset(&info, 0, sizeof(info));
    const uint32_t header_size = rd_u32(pkg + 8);
    info.section_count = rd_u32(pkg + 12);
    info.total_size = rd_u32(pkg + 16);
    info.model_version = rd_u32(pkg + 20);
    info.fw_api_min = rd_u16(pkg + 24);
    info.fw_api_max = rd_u16(pkg + 26);
    memcpy(info.model_id, pkg + 28, kPkgModelIdLen);
    info.model_id[kPkgModelIdLen] = '\0';
    info.flags = rd_u32(pkg + 60);
    if (header_size != kPkgHeaderSize || info.section_count == 0 || info.section_count > kPkgMaxSections) {
        return PkgError::Header;
    }
    const size_t table_end = kPkgHeaderSize + kPkgSectionEntrySize * info.section_count;
    if (info.total_size < table_end || info.total_size > len || info.model_id[0] == '\0') {
        return PkgError::Header;
    }
    for (uint32_t i = 0; i < info.section_count; i++) {
        const uint8_t *e = pkg + kPkgHeaderSize + kPkgSectionEntrySize * i;
        PkgSection &s = info.sections[i];
        s.type = rd_u32(e);
        s.offset = rd_u32(e + 4);
        s.length = rd_u32(e + 8);
        memcpy(s.sha256, e + 16, 32);
        if (s.offset < table_end || s.offset % 16 != 0 || s.length > info.total_size ||
            s.offset > info.total_size - s.length) {
            return PkgError::Sections;
        }
        for (uint32_t j = 0; j < i; j++) {
            const PkgSection &o = info.sections[j];
            if (o.type == s.type || (s.offset < o.offset + o.length && o.offset < s.offset + s.length)) {
                return PkgError::Sections;
            }
        }
    }
    if (!info.find(kSectionContract) || !info.find(kSectionTflite) || !info.find(kSectionTestVectors)) {
        return PkgError::Sections;
    }
    // Signature trailer right after total_size
    if (len < (size_t)info.total_size + 8) {
        return PkgError::Signature;
    }
    const uint8_t *t = pkg + info.total_size;
    if (memcmp(t, "PSG1", 4) != 0) {
        return PkgError::Signature;
    }
    info.sig_alg = rd_u16(t + 4);
    info.sig_len = rd_u16(t + 6);
    if (info.sig_len > kPkgSigMax || len < (size_t)info.total_size + 8 + info.sig_len ||
        (info.sig_alg == kSigAlgNone && info.sig_len != 0) ||
        (info.sig_alg != kSigAlgNone && info.sig_alg != kSigAlgEcdsaP256)) {
        return PkgError::Signature;
    }
    memcpy(info.sig, t + 8, info.sig_len);
    *out = info;
    return PkgError::Ok;
}

PkgError check_section_hashes(const uint8_t *pkg, const PkgInfo &info)
{
    for (uint32_t i = 0; i < info.section_count; i++) {
        const PkgSection &s = info.sections[i];
        uint8_t digest[32];
        sha256(pkg + s.offset, s.length, digest);
        if (memcmp(digest, s.sha256, 32) != 0) {
            return PkgError::SectionHash;
        }
    }
    return PkgError::Ok;
}

PkgError check_signature(const PkgInfo &info, const uint8_t digest[32], const char *public_key_pem)
{
    if (public_key_pem == nullptr) {
        return PkgError::Ok;
    }
    if (info.sig_alg != kSigAlgEcdsaP256 || info.sig_len == 0) {
        return PkgError::Signature;
    }
    return ecdsa_p256_verify(public_key_pem, digest, info.sig, info.sig_len) ? PkgError::Ok : PkgError::Signature;
}

PkgError check_compatibility(const PkgInfo &info, uint32_t firmware_model_api)
{
    if (firmware_model_api < info.fw_api_min || firmware_model_api > info.fw_api_max) {
        return PkgError::Incompatible;
    }
    return PkgError::Ok;
}

} // namespace presence
