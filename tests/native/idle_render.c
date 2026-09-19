#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

struct obs_core_video_mix { bool texture_rendered, clipper_native_pending; };
static struct {
    struct { struct { unsigned num; } mixes; } video;
    struct {
        pthread_mutex_t draw_callbacks_mutex, displays_mutex;
        struct { unsigned num; } draw_callbacks, rendered_callbacks;
        void *first_display;
    } data;
} core = {
    .video.mixes.num = 1,
    .data.draw_callbacks_mutex = PTHREAD_MUTEX_INITIALIZER,
    .data.displays_mutex = PTHREAD_MUTEX_INITIALIZER,
}, *obs = &core;

#include "idle_render_under_test.h"

int main(void)
{
    unsetenv("CLIPPER_IDLE_RENDER");
    struct obs_core_video_mix mix = {.texture_rendered = true};
    assert(clipper_defer_idle_render(&mix, false, false));
    assert(!mix.texture_rendered && mix.clipper_native_pending);
    mix.texture_rendered = true;
    mix.clipper_native_pending = false;
    assert(!clipper_defer_idle_render(&mix, true, false));
    assert(!clipper_defer_idle_render(&mix, false, true));
#define REJECT(field, value) do { \
    __typeof__(field) saved = field; field = value; \
    assert(!clipper_defer_idle_render(&mix, false, false)); field = saved; \
} while (0)
    REJECT(core.video.mixes.num, 2);
    REJECT(core.data.draw_callbacks.num, 1);
    REJECT(core.data.rendered_callbacks.num, 1);
    REJECT(core.data.first_display, &mix);
    setenv("CLIPPER_IDLE_RENDER", "0", 1);
    assert(!clipper_defer_idle_render(&mix, false, false));
    assert(mix.texture_rendered && !mix.clipper_native_pending);
    unsetenv("CLIPPER_IDLE_RENDER");
    return 0;
}
