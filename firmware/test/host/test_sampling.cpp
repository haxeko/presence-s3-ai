// 5 Hz sampler, offline buffer and the JSON batch encoding.
#include "sample_codec.h"
#include "sample_ring.h"
#include "sampler.h"
#include "test_util.h"

using namespace presence;

namespace {

ld2410::Report engineering(uint8_t base, uint8_t state = 3)
{
    ld2410::Report r;
    memset(&r, 0, sizeof(r));
    r.type = ld2410::kTypeEngineering;
    r.engineering = true;
    r.target_state = state;
    r.max_moving_gate = 8;
    r.max_still_gate = 8;
    r.moving_cm = 100 + base;
    for (int g = 0; g < kGates; g++) {
        r.moving[g] = (uint8_t)(base + g);
        r.still[g] = (uint8_t)(base + 2 * g);
    }
    r.aux_len = 2;
    r.aux[0] = 0x7F;
    r.aux[1] = 1;
    return r;
}

void test_sampler()
{
    test_begin("sampler: per-channel maximum, last report fields, capped at 100");
    Sampler s;
    Sample out;
    ld2410::Report a = engineering(10, 1);
    ld2410::Report b = engineering(5, 2);
    b.moving[0] = 200; // out of range: capped
    b.still[8] = 0;
    CHECK(s.add_report(a));
    CHECK(s.add_report(b));
    CHECK(s.close_tick(0, &out));
    CHECK_EQ(out.seq, 0u);
    CHECK_EQ(out.tick, 0u);
    CHECK_EQ((int)out.n_frames, 2);
    CHECK_EQ((int)out.energy[0], 100);
    CHECK_EQ((int)out.energy[1], 11);              // max(11, 6)
    CHECK_EQ((int)out.energy[kGates + 8], 26);     // max(26, 0)
    CHECK_EQ((int)out.target_state, 2);            // last report
    CHECK_EQ((int)out.moving_cm, 105);
    CHECK(out.light == 0x7F && out.out_pin == 1);
    CHECK(out.info.pred == -1 && out.info.occ_permille == -1);

    test_begin("sampler: empty bin gives no sample, old values are never repeated");
    CHECK(!s.close_tick(1, &out));
    CHECK_EQ(s.empty_ticks(), 1u);
    ld2410::Report basic = engineering(1);
    basic.engineering = false;
    basic.type = ld2410::kTypeBasic;
    ld2410::Report three = engineering(1);
    three.max_still_gate = 2;
    CHECK(!s.add_report(basic));
    CHECK(!s.add_report(three));
    CHECK(!s.close_tick(2, &out));
    CHECK_EQ(s.reports_rejected(), 2u);
    CHECK(s.add_report(a));
    CHECK(s.close_tick(3, &out));
    CHECK(out.seq == 1 && out.tick == 3 && out.energy[0] == 10);
}

Sample make(uint32_t seq)
{
    Sample s;
    memset(&s, 0, sizeof(s));
    s.seq = seq;
    s.tick = seq * 2;
    return s;
}

void test_ring()
{
    test_begin("ring: drop-oldest when full, consume by sequence");
    std::vector<Sample> storage(5);
    SampleRing ring(storage.data(), storage.size());
    for (uint32_t i = 0; i < 3; i++) {
        ring.push(make(i));
    }
    Sample out[8];
    CHECK_EQ(ring.peek(out, 8), 3u);
    CHECK(out[0].seq == 0 && out[2].seq == 2);
    ring.consume_through(1);
    CHECK_EQ(ring.size(), 1u);
    for (uint32_t i = 3; i < 10; i++) {
        ring.push(make(i));
    }
    CHECK_EQ(ring.size(), 5u);
    CHECK_EQ(ring.dropped(), 3u); // 2, 3, 4 overwritten
    CHECK_EQ(ring.peek(out, 2), 2u);
    CHECK(out[0].seq == 5 && out[1].seq == 6);
    CHECK_EQ(ring.high_water(), 5u);
    // Batch peeked, then overwritten while being sent: consuming through its last seq is still right
    ring.push(make(10));
    ring.push(make(11));
    ring.consume_through(6);
    CHECK_EQ(ring.peek(out, 8), 5u);
    CHECK(out[0].seq == 7 && out[4].seq == 11);
    CHECK_EQ(ring.peek_from(9, out, 8), 3u); // 9, 10, 11
    CHECK(out[0].seq == 9 && out[2].seq == 11);
    CHECK_EQ(ring.peek_from(3, out, 2), 2u); // older than the oldest: from the oldest
    CHECK(out[0].seq == 7);
    CHECK_EQ(ring.peek_from(12, out, 8), 0u);
    ring.consume_through(100);
    CHECK_EQ(ring.size(), 0u);

    test_begin("ring: sequence counter wrap");
    SampleRing r2(storage.data(), storage.size());
    r2.push(make(0xFFFFFFFEu));
    r2.push(make(0xFFFFFFFFu));
    r2.push(make(0));
    r2.consume_through(0xFFFFFFFFu);
    CHECK_EQ(r2.size(), 1u);
    CHECK(r2.peek(out, 1) == 1 && out[0].seq == 0);
}

void test_codec()
{
    test_begin("codec: JSON batch with 40 columns per row, truncated to the buffer");
    Sample s[3];
    for (int i = 0; i < 3; i++) {
        s[i] = make(i);
        s[i].n_frames = 2;
        s[i].energy[0] = 99;
        s[i].energy[17] = 7;
        s[i].light = -1;
        s[i].out_pin = -1;
        s[i].info.pred = -1;
        s[i].info.occ_permille = -1;
    }
    s[1].info = DecisionInfo{ 1, kMethodModel, 42, 2, { -128, -100, 90, 10 }, 930, kStateOccupied, kDecisionOccupied };
    BatchHeader h{ "s3-a1b2c3", 0xDEADBEEF, 5000, 98000, 1761400000123ULL };
    char buf[2048];
    size_t len = 0;
    CHECK_EQ(encode_batch(h, s, 3, buf, sizeof(buf), &len), 3u);
    CHECK_EQ(len, strlen(buf));
    const std::string j(buf);
    CHECK(starts_with(j, "{\"v\":1,\"dev\":\"s3-a1b2c3\",\"boot\":3735928559,\"t0\":5000,\"now\":98000,"
                         "\"epoch\":1761400000123,\"rows\":[[0,0,2,"));
    CHECK(j.find("[1,2,2,0,0,0,0,0,0,99,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,7,-1,-1,1,3,42,2,-128,-100,90,10,930,2,2]") !=
          std::string::npos);
    CHECK(j.substr(j.size() - 2) == "]}");
    // Each row has 40 integers
    const size_t first = j.find("[[") + 1;
    const size_t end = j.find(']', first);
    CHECK_EQ(split(j.substr(first + 1, end - first - 1), ',').size(), (size_t)kRowColumns);
    // Small buffer: fewer rows, still valid JSON
    const size_t header_len = j.find("[[") + 1;
    const size_t row1_len = j.find("],[") + 1 - header_len;
    char small[400];
    CHECK_EQ(encode_batch(h, s, 3, small, header_len + row1_len + 4, &len), 1u);
    CHECK(std::string(small) == j.substr(0, header_len + row1_len) + "]}");
    CHECK_EQ(encode_batch(h, s, 3, small, header_len + row1_len + 3, &len), 0u);
    CHECK(std::string(small) == j.substr(0, header_len) + "]}");
    CHECK_EQ(encode_batch(h, s, 3, small, 40, &len), 0u);
}

} // namespace

void run_sampling_tests()
{
    test_sampler();
    test_ring();
    test_codec();
}
