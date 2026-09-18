/* SPDX-License-Identifier: GPL-2.0-or-later
 * Shared by the portable hooks and receiver. No OBS/graphics dependencies.
 * Wire extension lives entirely in the old messages' reserved padding.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define CLIPPER_COPY_CAPABILITY "CLPCPY01"
#define CLIPPER_COPY_CONTROL "CPY1"
#define CLIPPER_COPY_TIMING_CAPABILITY "CLPCPY02"
#define CLIPPER_COPY_TIMING "CPY2"

static inline uint32_t clipper_read_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static inline void clipper_write_le32(uint8_t *p, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i)
        p[i] = (uint8_t)(value >> (8 * i));
}

static inline bool clipper_copy_rate_valid(uint32_t num, uint32_t den)
{
    return den && den <= 100000 && num >= den && (uint64_t)num <= 1000ULL * den;
}

static inline void clipper_copy_control(uint8_t padding[12], uint32_t num, uint32_t den)
{
    memset(padding, 0, 12);
    if (!clipper_copy_rate_valid(num, den))
        return;
    memcpy(padding, CLIPPER_COPY_CONTROL, 4);
    clipper_write_le32(padding + 4, num);
    clipper_write_le32(padding + 8, den);
}

struct clipper_copy_clock {
    uint64_t period;
    uint64_t next;
    uint64_t last;
    bool started;
    uint32_t consumer_period_us;
    uint32_t deadline_us;
    uint32_t copied_deadline_us;
    uint64_t last_copy;
    bool copied;
};

/* Both peers use CLOCK_MONOTONIC. Microsecond timestamps wrap after 71 min;
 * signed modular differences are only used within a two-period window. */
static inline void clipper_copy_timing(uint8_t padding[12], uint32_t num,
                                      uint32_t den, uint64_t next_frame_ns)
{
    clipper_copy_control(padding, num, den);
    if (!clipper_copy_rate_valid(num, den))
        return;
    memcpy(padding, CLIPPER_COPY_TIMING, 4);
    clipper_write_le32(padding + 4, (1000000ULL * den + num - 1) / num);
    clipper_write_le32(padding + 8, (uint32_t)(next_frame_ns / 1000));
}

static inline void clipper_copy_configure(struct clipper_copy_clock *clock,
                                          const uint8_t padding[12])
{
    uint32_t num = clipper_read_le32(padding + 4);
    uint32_t den = clipper_read_le32(padding + 8);
    uint64_t period = 0;
    if (!memcmp(padding, CLIPPER_COPY_TIMING, 4) && num >= 1000 && num <= 1000000) {
        if (clock->consumer_period_us != num) {
            memset(clock, 0, sizeof(*clock));
            clock->consumer_period_us = num;
            clock->period = (uint64_t)num * 500;
        }
        clock->deadline_us = den;
        return;
    }
    if (!memcmp(padding, CLIPPER_COPY_CONTROL, 4) && clipper_copy_rate_valid(num, den)) {
        /* Twice consumer FPS provides phase/jitter headroom. Round up by at
         * most one nanosecond; fixed deadlines avoid present-to-present drift. */
        period = (1000000000ULL * den + 2ULL * num - 1) / (2ULL * num);
    }
    if (period != clock->period || clock->consumer_period_us) {
        memset(clock, 0, sizeof(*clock));
        clock->period = period;
    }
}

static inline bool clipper_copy_due(struct clipper_copy_clock *clock, uint64_t now)
{
    if (!clock->period)
        return true;
    if (clock->consumer_period_us && clock->started && now >= clock->last &&
        now - clock->last < clock->period) {
        int32_t until = (int32_t)(clock->deadline_us - (uint32_t)(now / 1000));
        /* A current consumer deadline grants one copy in its last half-frame.
         * A late/stalled consumer cannot hold a stale image indefinitely.
         * Slow games use the original scheduler and preserve every present. */
        if (until >= -(int32_t)clock->consumer_period_us &&
            until <= (int32_t)clock->consumer_period_us) {
            clock->last = now;
            if (until > (int32_t)(clock->consumer_period_us / 2))
                return false;
            if (clock->copied && clock->copied_deadline_us == clock->deadline_us &&
                now >= clock->last_copy &&
                now - clock->last_copy < (uint64_t)clock->consumer_period_us * 1000)
                return false;
            clock->copied = true;
            clock->copied_deadline_us = clock->deadline_us;
            clock->last_copy = now;
            clock->next = now + clock->period;
            return true;
        }
    }
    if (!clock->started || now < clock->last || now - clock->last > 1000000000ULL) {
        clock->started = true;
        clock->last = now;
        clock->next = now <= UINT64_MAX - clock->period ? now + clock->period : now;
        return true;
    }
    clock->last = now;
    if (now < clock->next)
        return false;
    /* Advance past missed slots in constant time; never submit catch-up work. */
    uint64_t remaining = clock->period - (now - clock->next) % clock->period;
    clock->next = now <= UINT64_MAX - remaining ? now + remaining : now;
    return true;
}
