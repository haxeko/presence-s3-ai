// Minimal test helpers for the host tests (no ESP-IDF, no test framework dependency).
#pragma once

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

extern int g_failures;
extern int g_checks;
extern const char *g_test;

void test_begin(const char *name);
void test_fail(const char *file, int line, const char *expr);
int test_summary();

#define CHECK(cond)                                                                                                \
    do {                                                                                                           \
        g_checks++;                                                                                                \
        if (!(cond)) {                                                                                             \
            test_fail(__FILE__, __LINE__, #cond);                                                                  \
        }                                                                                                          \
    } while (0)

#define CHECK_EQ(a, b)                                                                                             \
    do {                                                                                                           \
        g_checks++;                                                                                                \
        const auto va_ = (a);                                                                                      \
        const auto vb_ = (b);                                                                                      \
        if (!(va_ == vb_)) {                                                                                       \
            fprintf(stderr, "    %s = %lld, %s = %lld\n", #a, (long long)va_, #b, (long long)vb_);                 \
            test_fail(__FILE__, __LINE__, #a " == " #b);                                                           \
        }                                                                                                          \
    } while (0)

std::string fixture_path(const std::string &rel);
std::vector<std::string> read_lines(const std::string &path);
std::vector<uint8_t> read_file(const std::string &path);
std::string read_text(const std::string &path);
std::vector<std::string> split(const std::string &s, char sep = ' ');
bool starts_with(const std::string &s, const char *prefix);
std::vector<uint8_t> hex_bytes(const std::string &hex);
float hex_f32(const std::string &hex); // little-endian float32 bit pattern
uint32_t f32_bits(float f);
