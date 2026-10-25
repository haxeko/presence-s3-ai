#include "test_util.h"

#include <stdlib.h>

#include <fstream>
#include <sstream>

int g_failures = 0;
int g_checks = 0;
const char *g_test = "";

void test_begin(const char *name)
{
    g_test = name;
    printf("  %s\n", name);
}

void test_fail(const char *file, int line, const char *expr)
{
    g_failures++;
    fprintf(stderr, "FAIL [%s] %s:%d: %s\n", g_test, file, line, expr);
}

int test_summary()
{
    printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}

std::string fixture_path(const std::string &rel)
{
    const char *dir = getenv("PRESENCE_FIXTURES");
    return std::string(dir ? dir : FIXTURE_DIR) + "/" + rel;
}

std::vector<std::string> read_lines(const std::string &path)
{
    std::ifstream f(path);
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path.c_str());
        exit(2);
    }
    std::vector<std::string> lines;
    std::string line;
    while (std::getline(f, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty() || line[0] == '#') {
            continue;
        }
        lines.push_back(line);
    }
    return lines;
}

std::vector<uint8_t> read_file(const std::string &path)
{
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path.c_str());
        exit(2);
    }
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

std::string read_text(const std::string &path)
{
    const std::vector<uint8_t> d = read_file(path);
    return std::string(d.begin(), d.end());
}

std::vector<std::string> split(const std::string &s, char sep)
{
    std::vector<std::string> out;
    std::string cur;
    std::istringstream in(s);
    while (std::getline(in, cur, sep)) {
        if (!cur.empty()) {
            out.push_back(cur);
        }
    }
    return out;
}

bool starts_with(const std::string &s, const char *prefix)
{
    return s.compare(0, strlen(prefix), prefix) == 0;
}

std::vector<uint8_t> hex_bytes(const std::string &hex)
{
    std::vector<uint8_t> out;
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        out.push_back((uint8_t)strtoul(hex.substr(i, 2).c_str(), nullptr, 16));
    }
    return out;
}

float hex_f32(const std::string &hex)
{
    const std::vector<uint8_t> b = hex_bytes(hex);
    uint32_t u = (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
    float f;
    memcpy(&f, &u, sizeof(f));
    return f;
}

uint32_t f32_bits(float f)
{
    uint32_t u;
    memcpy(&u, &f, sizeof(u));
    return u;
}
