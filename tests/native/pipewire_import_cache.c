/* Exercise the real patch helpers without a running compositor or GPU. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_AV_PLANES 8
#define GS_BGRX 1
#define PW_CHECK_VERSION(a, b, c) EXPLICIT_SYNC
#define g_clear_pointer(p, destroy) do { destroy(*(p)); *(p) = NULL; } while (0)
#define bfree free

typedef unsigned gs_texture_t;
struct pw_buffer { unsigned identity; };
struct dmabuf_import;
typedef struct {
    gs_texture_t *texture;
    bool texture_imported;
    struct dmabuf_import *imports;
    struct { struct { struct { struct { unsigned width, height; } size; } raw; } info; } format;
    struct {
        int acquire_syncobj_fd, release_syncobj_fd;
        uint64_t release_point;
        bool release_point_will_signal, set;
    } sync;
} obs_pipewire_stream;

static unsigned attempts, live, destroyed, signaled, closed, graphics_depth;
static bool fail_import;
static void *bzalloc(size_t size) { void *p = calloc(1, size); assert(p); return p; }
static void obs_enter_graphics(void) { graphics_depth++; }
static void obs_leave_graphics(void) { assert(graphics_depth); graphics_depth--; }
static void g_clear_fd(int *fd, void *unused)
{
    (void)unused;
    if (*fd != -1) { closed++; *fd = -1; }
}
#if EXPLICIT_SYNC
static void gs_sync_signal_syncobj_timeline_point(int fd, uint64_t point)
{
    assert(graphics_depth && fd == 12 && point == 42);
    signaled++;
}
#endif
static void gs_texture_destroy(gs_texture_t *texture)
{
    assert(graphics_depth);
    if (texture) { assert(live); live--; destroyed++; free(texture); }
}
static gs_texture_t *gs_texture_create_from_dmabuf(unsigned w, unsigned h, uint32_t format,
    int color, uint32_t planes, const int *fds, const uint32_t *strides,
    const uint32_t *offsets, const uint64_t *modifiers)
{
    assert(graphics_depth && w && h && format == 1 && color == GS_BGRX);
    assert(planes && fds && strides && offsets);
    (void)modifiers;
    attempts++;
    if (fail_import) return NULL;
    gs_texture_t *texture = bzalloc(sizeof(*texture));
    *texture = attempts;
    live++;
    return texture;
}

#include "pipewire_cache_under_test.h"

static void select_buffer(obs_pipewire_stream *s, struct pw_buffer *b,
                          unsigned planes, int *fds, uint32_t *strides, uint32_t *offsets)
{
    clear_current_texture(s);
    s->texture = import_buffer(s, b, 1, planes, fds, strides, offsets, NULL);
    s->texture_imported = s->texture != NULL;
}

int main(void)
{
    obs_pipewire_stream s = {0};
    s.sync.acquire_syncobj_fd = s.sync.release_syncobj_fd = -1;
    s.format.info.raw.size.width = 1920;
    s.format.info.raw.size.height = 1080;
    struct pw_buffer a = {1}, b = {2};
    int fds[] = {5, 6};
    uint32_t strides[] = {7680, 3840}, offsets[] = {0, 4096};
    obs_enter_graphics();

    /* Alternating live buffers import once each, even with identical fds. */
    for (unsigned i = 0; i < 100; i++)
        select_buffer(&s, i % 2 ? &a : &b, 2, fds, strides, offsets);
    assert(attempts == 2 && live == 2 && destroyed == 0);

    offsets[1]++;
    select_buffer(&s, &a, 2, fds, strides, offsets);
    strides[0]++;
    select_buffer(&s, &a, 2, fds, strides, offsets);
    fds[0]++;
    select_buffer(&s, &a, 2, fds, strides, offsets);
    select_buffer(&s, &a, 1, fds, strides, offsets);
    assert(attempts == 6 && live == 2 && destroyed == 4);
    assert(!import_buffer(&s, &a, 1, 0, fds, strides, offsets, NULL));
    assert(!import_buffer(&s, &a, 1, MAX_AV_PLANES + 1, fds, strides, offsets, NULL));

    /* Removing another buffer preserves the displayed texture and its fence. */
    s.sync.acquire_syncobj_fd = 11;
    s.sync.release_syncobj_fd = 12;
    s.sync.release_point = 42;
    s.sync.set = true;
    on_remove_buffer_cb(&s, &b);
    assert(live == 1 && s.texture && s.sync.set && !closed && !signaled);
    on_remove_buffer_cb(&s, &a);
    assert(!live && !s.texture && !s.imports && !s.sync.set && closed == 2);
    assert(signaled == EXPLICIT_SYNC);
    on_remove_buffer_cb(&s, &a); /* No duplicate destruction/signaling. */
    assert(signaled == EXPLICIT_SYNC);

    /* Pointer and fd reuse after removal must import again. */
    select_buffer(&s, &a, 1, fds, strides, offsets);
    assert(attempts == 7 && live == 1);
    s.sync.acquire_syncobj_fd = 11;
    s.sync.release_syncobj_fd = 12;
    s.sync.release_point_will_signal = true;
    clear_imports(&s); /* Format change or teardown; GPU owns this release. */
    assert(!live && !s.texture && !s.imports && closed == 4);
    assert(signaled == EXPLICIT_SYNC);
    s.format.info.raw.size.width = 1280;
    select_buffer(&s, &a, 1, fds, strides, offsets);
    assert(attempts == 8 && live == 1);

    /* Failed imports never become reusable cache hits; retry can succeed. */
    fail_import = true;
    select_buffer(&s, &b, 1, fds, strides, offsets);
    assert(!s.texture && live == 1);
    fail_import = false;
    select_buffer(&s, &b, 1, fds, strides, offsets);
    assert(s.texture && attempts == 10 && live == 2);

    /* SHM owns its texture; changing paths must not destroy a cached import. */
    clear_current_texture(&s);
    assert(live == 2);
    s.texture = bzalloc(sizeof(*s.texture));
    live++;
    clear_imports(&s);
    assert(!live && !s.texture && !s.imports);
    clear_imports(&s);
    obs_leave_graphics();
    assert(!graphics_depth);
}
