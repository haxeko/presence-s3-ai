// Model package v1 ("PSAIPKG1"), built by training/presence_ml/package.py. Little-endian layout:
//
//   0   magic "PSAIPKG1"            8
//   8   header_size (= 64)          4
//   12  section_count (<= 8)        4
//   16  total_size                  4   bytes covered by the hash and the signature
//   20  model_version               4   increases with every release
//   24  fw_api_min, fw_api_max      2+2 firmware model API range accepted (this firmware: kFirmwareModelApi)
//   28  model_id                    32  NUL-padded
//   60  flags                       4   bit 0: trained on simulated data (pipeline test only)
//   64  section table               48 per section: type, offset, length, reserved (4 each), sha256 (32)
//   ... sections, 16-byte aligned: 1 CONTRACT (888 bytes), 2 TFLITE, 3 TESTVEC, 4 MANIFEST (JSON, information)
//   total_size: signature trailer "PSG1", alg (2: 0 none, 1 ECDSA P-256 SHA-256), sig_len (2), DER signature
//
// The signature covers SHA-256(bytes [0, total_size)).
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace presence {

constexpr size_t kPkgHeaderSize = 64;
constexpr size_t kPkgSectionEntrySize = 48;
constexpr uint32_t kPkgMaxSections = 8;
constexpr size_t kPkgModelIdLen = 32;
constexpr size_t kPkgSigMax = 80;

constexpr uint32_t kSectionContract = 1;
constexpr uint32_t kSectionTflite = 2;
constexpr uint32_t kSectionTestVectors = 3;
constexpr uint32_t kSectionManifest = 4;

constexpr uint32_t kPkgFlagSimulated = 1u << 0;

constexpr uint16_t kSigAlgNone = 0;
constexpr uint16_t kSigAlgEcdsaP256 = 1;

struct PkgSection {
    uint32_t type;
    uint32_t offset;
    uint32_t length;
    uint8_t sha256[32];
};

struct PkgInfo {
    uint32_t total_size;
    uint32_t model_version;
    uint16_t fw_api_min;
    uint16_t fw_api_max;
    char model_id[kPkgModelIdLen + 1];
    uint32_t flags;
    uint32_t section_count;
    PkgSection sections[kPkgMaxSections];
    uint16_t sig_alg;
    uint16_t sig_len;
    uint8_t sig[kPkgSigMax];

    const PkgSection *find(uint32_t type) const;
};

enum class PkgError {
    Ok = 0,
    TooSmall,
    Magic,
    Header,
    Sections,     // table out of range, overlap, missing required section
    SectionHash,
    Signature,    // missing, malformed or wrong signature
    Incompatible, // firmware model API outside the accepted range
};

const char *pkg_error_name(PkgError e);

// Parses header, section table and signature trailer of a complete package in memory (no hash checks).
PkgError parse_package(const uint8_t *pkg, size_t len, PkgInfo *out);
// Checks every section hash. Needs the whole package in memory.
PkgError check_section_hashes(const uint8_t *pkg, const PkgInfo &info);
// Checks the signature of the package digest (SHA-256 of [0, total_size)). public_key_pem == nullptr: the
// firmware accepts unsigned packages (integrity is then only checked against the expected SHA-256).
PkgError check_signature(const PkgInfo &info, const uint8_t digest[32], const char *public_key_pem);
PkgError check_compatibility(const PkgInfo &info, uint32_t firmware_model_api);

} // namespace presence
