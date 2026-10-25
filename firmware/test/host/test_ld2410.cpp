// LD2410 parser against the Python parser (fixture ld2410_frames.txt) and the command helpers.
#include "ld2410_parser.h"
#include "ld2410_protocol.h"
#include "test_util.h"

using namespace ld2410;

namespace {

struct Collected {
    std::vector<std::string> events;
};

std::string join_u8(const uint8_t *v, int n)
{
    std::string s;
    for (int i = 0; i < n; i++) {
        s += (i ? "," : "") + std::to_string(v[i]);
    }
    return s.empty() ? "-" : s;
}

void on_report(const Report &r, void *ctx)
{
    char aux[2 * kMaxAux + 2] = "-";
    if (r.aux_len) {
        for (int i = 0; i < r.aux_len; i++) {
            snprintf(aux + 2 * i, 3, "%02x", r.aux[i]);
        }
    }
    char line[256];
    snprintf(line, sizeof(line), "report %u %u %u %u %u %u %u %d %u %u %s %s %s", r.type, r.target_state, r.moving_cm,
             r.moving_energy, r.still_cm, r.still_energy, r.detect_cm, r.engineering ? 1 : 0, r.max_moving_gate,
             r.max_still_gate, r.engineering ? join_u8(r.moving, r.max_moving_gate + 1).c_str() : "-",
             r.engineering ? join_u8(r.still, r.max_still_gate + 1).c_str() : "-", aux);
    ((Collected *)ctx)->events.push_back(line);
}

void on_ack(const Ack &a, void *ctx)
{
    char data[2 * kMaxAckData + 2] = "-";
    for (int i = 0; i < a.len; i++) {
        snprintf(data + 2 * i, 3, "%02x", a.data[i]);
    }
    char line[128];
    snprintf(line, sizeof(line), "ack %u %u %s", a.command, a.status, data);
    ((Collected *)ctx)->events.push_back(line);
}

std::string stats_line(const ParserStats &s)
{
    char line[96];
    snprintf(line, sizeof(line), "stats %u %u %u %u %u", s.reports, s.acks, s.bad_length, s.bad_footer, s.bad_payload);
    return line;
}

void test_fixture()
{
    const std::vector<std::string> lines = read_lines(fixture_path("ld2410_frames.txt"));
    size_t i = 0;
    int cases = 0;
    while (i < lines.size()) {
        CHECK(starts_with(lines[i], "case "));
        test_begin(("ld2410 fixture: " + lines[i].substr(5)).c_str());
        const std::vector<uint8_t> stream = hex_bytes(lines[i + 1].substr(7));
        const std::vector<std::string> chunks = split(lines[i + 2].substr(7));
        std::vector<std::string> expected;
        std::string expected_stats;
        i += 3;
        while (lines[i] != "end") {
            if (starts_with(lines[i], "stats ")) {
                expected_stats = lines[i];
            } else {
                expected.push_back(lines[i]);
            }
            i++;
        }
        i++;
        cases++;

        // Whole stream at once
        Collected all;
        Parser p(on_report, on_ack, &all);
        p.feed(stream.data(), stream.size());
        CHECK(all.events == expected);
        CHECK(stats_line(p.stats()) == expected_stats);
        CHECK_EQ(p.stats().bytes, stream.size());
        if (all.events != expected) {
            for (size_t k = 0; k < all.events.size() || k < expected.size(); k++) {
                fprintf(stderr, "    got  %s\n    want %s\n", k < all.events.size() ? all.events[k].c_str() : "",
                        k < expected.size() ? expected[k].c_str() : "");
            }
        }

        // Same stream split into the fixture's random chunks, then byte by byte
        Collected chunked;
        Parser pc(on_report, on_ack, &chunked);
        size_t pos = 0;
        for (const std::string &c : chunks) {
            const size_t n = std::min((size_t)atoi(c.c_str()), stream.size() - pos);
            pc.feed(stream.data() + pos, n);
            pos += n;
        }
        CHECK(chunked.events == expected);
        Collected bytewise;
        Parser pb(on_report, on_ack, &bytewise);
        for (uint8_t b : stream) {
            pb.feed(&b, 1);
        }
        CHECK(bytewise.events == expected);
        CHECK(stats_line(pb.stats()) == expected_stats);
    }
    CHECK(cases >= 10);
}

void test_commands()
{
    test_begin("ld2410: command frames");
    uint8_t buf[32];
    static const uint8_t value[] = { 0x01, 0x00 };
    size_t n = build_command(kCmdEnableConfig, value, sizeof(value), buf, sizeof(buf));
    const uint8_t expected[] = { 0xFD, 0xFC, 0xFB, 0xFA, 0x04, 0x00, 0xFF, 0x00, 0x01, 0x00, 0x04, 0x03, 0x02, 0x01 };
    CHECK_EQ(n, sizeof(expected));
    CHECK(memcmp(buf, expected, sizeof(expected)) == 0);
    n = build_command(kCmdEngineeringOn, nullptr, 0, buf, sizeof(buf));
    const uint8_t eng[] = { 0xFD, 0xFC, 0xFB, 0xFA, 0x02, 0x00, 0x62, 0x00, 0x04, 0x03, 0x02, 0x01 };
    CHECK_EQ(n, sizeof(eng));
    CHECK(memcmp(buf, eng, sizeof(eng)) == 0);
    CHECK_EQ(build_command(kCmdEngineeringOn, nullptr, 0, buf, 8), 0u);

    test_begin("ld2410: ACK decoders");
    Ack a;
    memset(&a, 0, sizeof(a));
    a.command = kCmdReadParams | kAckFlag;
    const uint8_t params[] = { 0xAA, 8, 8, 6, 50, 50, 40, 30, 20, 15, 15, 15, 15, 0, 0, 40, 40, 30, 30, 20, 20, 20, 5, 0 };
    memcpy(a.data, params, sizeof(params));
    a.len = sizeof(params);
    RadarParams rp;
    CHECK(decode_params(a, &rp));
    CHECK(rp.max_gate == 8 && rp.max_moving_gate == 8 && rp.max_still_gate == 6);
    CHECK(rp.moving_threshold[0] == 50 && rp.still_threshold[8] == 20 && rp.no_one_duration_s == 5);
    a.status = 1;
    CHECK(!decode_params(a, &rp));
    a.status = 0;
    a.len = 10;
    CHECK(!decode_params(a, &rp));

    Ack v;
    memset(&v, 0, sizeof(v));
    v.command = kCmdReadVersion | kAckFlag;
    const uint8_t ver[] = { 0x00, 0x00, 0x44, 0x02, 0x17, 0x09, 0x07, 0x25 };
    memcpy(v.data, ver, sizeof(ver));
    v.len = sizeof(ver);
    RadarVersion rv;
    CHECK(decode_version(v, &rv));
    char text[24];
    format_version(rv, text, sizeof(text));
    CHECK(strcmp(text, "V2.44.25070917") == 0);

    Ack r;
    memset(&r, 0, sizeof(r));
    r.command = kCmdReadResolution | kAckFlag;
    r.len = 2;
    uint16_t cm = 0;
    CHECK(decode_resolution(r, &cm) && cm == 75);
    r.data[0] = 1;
    CHECK(decode_resolution(r, &cm) && cm == 20);
    r.data[0] = 7;
    CHECK(!decode_resolution(r, &cm));

    Ack e;
    memset(&e, 0, sizeof(e));
    e.command = kCmdEnableConfig | kAckFlag;
    const uint8_t en[] = { 0x01, 0x00, 0x40, 0x00 };
    memcpy(e.data, en, sizeof(en));
    e.len = 4;
    uint16_t proto = 0, bufsize = 0;
    CHECK(decode_enable_config(e, &proto, &bufsize) && proto == 1 && bufsize == 64);
}

} // namespace

void run_ld2410_tests()
{
    test_fixture();
    test_commands();
}
