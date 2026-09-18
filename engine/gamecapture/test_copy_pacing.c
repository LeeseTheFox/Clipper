#include "copy_pacing.h"
#include <assert.h>
#include <stdio.h>

static struct clipper_copy_clock make_clock(uint32_t num, uint32_t den)
{
    struct clipper_copy_clock clock = {0};
    uint8_t wire[12];
    clipper_copy_control(wire, num, den);
    clipper_copy_configure(&clock, wire);
    return clock;
}

static void test_rate(unsigned source)
{
    struct clipper_copy_clock clock = make_clock(60, 1);
    unsigned copies = 0;
    uint64_t previous = 0, largest_gap = 0;
    for (unsigned frame = 0; frame < source * 10; ++frame) {
        uint64_t now = (uint64_t)frame * 1000000000 / source;
        if (clipper_copy_due(&clock, now)) {
            if (copies && now - previous > largest_gap)
                largest_gap = now - previous;
            previous = now;
            copies++;
        }
    }
    unsigned expected = source <= 120 ? source * 10 : 1200;
    assert(copies >= expected - 1 && copies <= expected + 1);
    if (source >= 120)
        assert(largest_gap <= 16666668);
    printf("source=%u copies=%u gap_ns=%llu\n", source, copies,
           (unsigned long long)largest_gap);
}

static void test_consumer(unsigned source, uint64_t phase, bool jitter)
{
    struct clipper_copy_clock clock = {0};
    uint8_t wire[12];
    const uint64_t period = 16666667;
    uint64_t tick = phase, last_copy = 0, consumed = 0;
    unsigned copies = 0, frames = 0;
    clipper_copy_timing(wire, 60, 1, tick + period);
    clipper_copy_configure(&clock, wire);
    for (unsigned i = 0; i < source * 10; ++i) {
        uint64_t now = (uint64_t)i * 1000000000 / source;
        if (jitter && i % 2)
            now += 300000;
        while (now >= tick + period) {
            tick += period;
            if (source >= 144 && frames > 1) {
                assert(last_copy > consumed);
                assert(tick - last_copy <= period);
            }
            consumed = last_copy;
            frames++;
            clipper_copy_timing(wire, 60, 1, tick + period);
            clipper_copy_configure(&clock, wire);
        }
        if (clipper_copy_due(&clock, now)) {
            last_copy = now;
            copies++;
        }
    }
    if (source >= 144)
        assert(copies >= 598 && copies <= 603);
    if (source <= 60)
        assert(copies == source * 10);
}

static void test_consumer_lifecycle(void)
{
    uint8_t wire[12];
    struct clipper_copy_clock clock = {0};
    /* Cross the 32-bit microsecond wrap with a fresh deadline. */
    uint64_t start = ((uint64_t)UINT32_MAX - 8000) * 1000;
    clipper_copy_timing(wire, 60000, 1001, start + 16683333);
    clipper_copy_configure(&clock, wire);
    assert(clock.consumer_period_us == 16684);
    assert(clipper_copy_due(&clock, start));
    assert(!clipper_copy_due(&clock, start + 4000000));
    assert(!clipper_copy_due(&clock, start + 8000000));
    assert(clipper_copy_due(&clock, start + 12000000));
    clipper_copy_configure(&clock, wire); /* Duplicate controls grant no credit. */
    assert(!clipper_copy_due(&clock, start + 16000000));
    assert(!clipper_copy_due(&clock, start + 20000000));
    /* A stalled/delayed receiver falls back, never queues catch-up copies. */
    assert(clipper_copy_due(&clock, start + 100000000));
    assert(!clipper_copy_due(&clock, start + 100000001));
    assert(clipper_copy_due(&clock, 1));
    clipper_copy_control(wire, 60, 1);
    clipper_copy_configure(&clock, wire);
    assert(!clock.consumer_period_us && clock.period == 8333334);
    clipper_copy_timing(wire, 60, 1, 100000000);
    clipper_copy_configure(&clock, wire);
    clipper_write_le32(wire + 4, UINT32_MAX); /* Malformed timing disables pacing. */
    clipper_copy_configure(&clock, wire);
    assert(!clock.period && !clock.consumer_period_us);
}

int main(void)
{
    test_consumer_lifecycle();
    const unsigned rates[] = {30, 59, 60, 61, 90, 120, 144, 240, 360, 1000};
    for (unsigned i = 0; i < sizeof(rates) / sizeof(rates[0]); ++i)
        test_rate(rates[i]);
    for (unsigned i = 0; i < sizeof(rates) / sizeof(rates[0]); ++i)
        for (unsigned phase = 0; phase < 16000000; phase += 1000000) {
            test_consumer(rates[i], phase, false);
            test_consumer(rates[i], phase, true);
        }
    uint8_t wire[12] = {0};
    struct clipper_copy_clock clock = make_clock(60000, 1001);
    assert(clock.period == 8341667);
    assert(clipper_copy_due(&clock, 100));
    assert(!clipper_copy_due(&clock, 101));
    assert(clipper_copy_due(&clock, 5000000000ULL));
    assert(!clipper_copy_due(&clock, 5000000001ULL));
    assert(clipper_copy_due(&clock, 1)); /* Clock discontinuity. */
    assert(clipper_copy_due(&clock, UINT64_MAX - 2));
    assert(clipper_copy_due(&clock, 0));
    clipper_copy_configure(&clock, wire); /* Old receiver/disabled. */
    assert(!clock.period && clipper_copy_due(&clock, 0));
    assert(clipper_copy_due(&clock, 0));
    memcpy(wire, CLIPPER_COPY_CONTROL, 4);
    clipper_write_le32(wire + 4, UINT32_MAX);
    clipper_write_le32(wire + 8, 1);
    clipper_copy_configure(&clock, wire);
    assert(!clock.period);
    clipper_copy_control(wire, 60, 0);
    assert(!memcmp(wire, (uint8_t[12]){0}, 12));
    clipper_copy_control(wire, 60, 1);
    assert(wire[4] == 60 && wire[5] == 0 && wire[8] == 1);
    clipper_copy_configure(&clock, wire);
    assert(clipper_copy_due(&clock, 0));
    uint64_t next = clock.next;
    clipper_copy_configure(&clock, wire); /* Same controls retain phase. */
    assert(clock.next == next && clock.started);
    clipper_copy_control(wire, 30, 1);
    clipper_copy_configure(&clock, wire);
    assert(!clock.started && clipper_copy_due(&clock, 1));

    /* Near-target jitter must not skip frames; high-rate jitter stays bounded. */
    for (unsigned rate = 60; rate <= 240; rate += 180) {
        clock = make_clock(60, 1);
        uint64_t now = 0, last_copy = 0;
        unsigned copies = 0;
        for (unsigned i = 0; i < rate * 10; ++i) {
            now += 1000000000 / rate + (i % 2 ? 500000 : -500000);
            if (clipper_copy_due(&clock, now)) {
                assert(!copies || now - last_copy <= 18000000);
                last_copy = now;
                copies++;
            }
        }
        if (rate == 60)
            assert(copies == 600);
        else
            assert(copies >= 1198 && copies <= 1202);
    }
    puts("copy pacing tests passed");
}
