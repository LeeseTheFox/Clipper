#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>

typedef struct obs_source {
    bool enabled, removed;
    unsigned filters, width, height, refs, flags;
    const char *id;
    struct obs_scene *scene;
} obs_source_t;
#define MAX_CHANNELS 8
#define OBS_SOURCE_VIDEO 1
struct obs_view { obs_source_t *channels[MAX_CHANNELS]; };
static unsigned obs_source_get_output_flags(obs_source_t *s) { return s->flags; }
struct matrix4 { float v[16]; };
struct obs_scene_item {
    struct obs_scene_item *next;
    bool user_visible, visible, removed, is_group, is_scene;
    bool show_transition, hide_transition, update_transform, size_changed;
    bool texture, blend;
    long defer_update;
    int crop, bounds_crop;
    struct matrix4 draw_transform;
    obs_source_t *source;
};
typedef struct obs_scene {
    bool is_group;
    unsigned last_width, last_height;
    struct obs_scene_item *first_item;
} obs_scene_t;
static obs_scene_t *obs_scene_from_source(obs_source_t *s) { return s->scene; }
static bool obs_source_enabled(obs_source_t *s) { return s->enabled; }
static bool obs_source_removed(obs_source_t *s) { return s->removed; }
static unsigned obs_source_filter_count(obs_source_t *s) { return s->filters; }
static unsigned obs_source_get_width(obs_source_t *s) { return s->width; }
static unsigned obs_source_get_height(obs_source_t *s) { return s->height; }
static const char *obs_source_get_id(obs_source_t *s) { return s->id; }
static obs_source_t *obs_source_get_ref(obs_source_t *s) { s->refs++; return s; }
static void video_lock(obs_scene_t *s) { (void)s; }
static void video_unlock(obs_scene_t *s) { (void)s; }
static bool os_atomic_load_bool(bool *b) { return *b; }
static long os_atomic_load_long(long *n) { return *n; }
static bool source_size_changed(struct obs_scene_item *i) { return i->size_changed; }
static bool crop_enabled(int *n) { return *n != 0; }
static bool item_texture_enabled(struct obs_scene_item *i) { return i->texture; }
static bool default_blending_enabled(struct obs_scene_item *i) { return i->blend; }
static void matrix4_identity(struct matrix4 *m) {
    memset(m, 0, sizeof(*m));
    for (int i = 0; i < 16; i += 5) m->v[i] = 1;
}
enum gs_color_format { GS_RGBA, GS_BGRA, GS_BGRX, GS_RGBA16F };
typedef struct { enum gs_color_format format; } gs_texture_t;
static enum gs_color_format gs_texture_get_color_format(gs_texture_t *t) { return t->format; }
enum { VK_COLOR_SPACE_SRGB_NONLINEAR_KHR, HDR, IMPORT_LINEAR_HOST_MAPPED = 3 };
typedef struct { gs_texture_t *texture; bool flip; } calldata_t;
static void calldata_set_ptr(calldata_t *cd, const char *key, void *p) { (void)key; cd->texture = p; }
static void calldata_set_bool(calldata_t *cd, const char *key, bool b) { (void)key; cd->flip = b; }
typedef struct {
    gs_texture_t *texture;
    bool show_cursor, allow_transparency;
    struct { int color_space; bool flip; } tdata;
    int client_id, buf_id;
} vkcapture_source_t;
typedef struct { int buf_id, import_failures; void *map_memory; } vkcapture_client_t;
static struct { pthread_mutex_t mutex; } server = {PTHREAD_MUTEX_INITIALIZER};
static vkcapture_client_t client = {.buf_id = 1};
static vkcapture_client_t *find_client_by_id(int id) { return id == 1 ? &client : NULL; }

#include "native_capture_under_test.h"

int main(void)
{
    obs_source_t capture = {.enabled = true, .width = 2560, .height = 1440, .id = "vkcapture-source"};
    struct obs_scene_item item = {.user_visible = true, .visible = true, .blend = true, .source = &capture};
    matrix4_identity(&item.draw_transform);
    item.draw_transform.v[12] = -0.0f;
    obs_scene_t scene = {.last_width = 2560, .last_height = 1440, .first_item = &item};
    obs_source_t source = {.enabled = true, .scene = &scene};
    obs_source_t audio = {.flags = 2};
    obs_source_t overlay = {.flags = OBS_SOURCE_VIDEO};
    struct obs_view view = {.channels = {&source, &audio}};
    assert(native_capture_single_video_channel(&view));
    view.channels[MAX_CHANNELS - 1] = &overlay;
    assert(!native_capture_single_video_channel(&view));
    view.channels[MAX_CHANNELS - 1] = NULL;
    view.channels[0] = NULL;
    assert(!native_capture_single_video_channel(&view));
    assert(obs_scene_native_capture(&source, 2560, 1440) == &capture);
    assert(capture.refs == 1);
#define REJECT(field, value) do { \
    __typeof__(field) saved = field; field = value; \
    assert(!obs_scene_native_capture(&source, 2560, 1440)); field = saved; \
} while (0)
    REJECT(source.filters, 1);
    REJECT(source.enabled, false);
    REJECT(source.removed, true);
    REJECT(scene.is_group, true);
    REJECT(scene.last_width, 1280);
    REJECT(scene.first_item, NULL);
    REJECT(item.next, &item);
    REJECT(item.visible, false);
    REJECT(item.user_visible, false);
    REJECT(item.removed, true);
    REJECT(item.is_group, true);
    REJECT(item.is_scene, true);
    REJECT(item.show_transition, true);
    REJECT(item.hide_transition, true);
    REJECT(item.update_transform, true);
    REJECT(item.defer_update, 1);
    REJECT(item.size_changed, true);
    REJECT(item.crop, 1);
    REJECT(item.bounds_crop, 1);
    REJECT(item.texture, true);
    REJECT(item.blend, false);
    REJECT(item.draw_transform.v[0], 0.5f);
    REJECT(item.draw_transform.v[12], 1.0f);
    REJECT(capture.enabled, false);
    REJECT(capture.removed, true);
    REJECT(capture.filters, 1);
    REJECT(capture.width, 1280);
    REJECT(capture.height, 720);
    REJECT(capture.id, "other");
    assert(capture.refs == 1);
#undef REJECT
    /* Full-frame up/downscales qualify, but letterboxing and transforms do not. */
    capture.width = 960;
    capture.height = 540;
    item.draw_transform.v[0] = 2560.0f / 960;
    item.draw_transform.v[5] = 1440.0f / 540;
    assert(obs_scene_native_capture(&source, 2560, 1440) == &capture);
    item.draw_transform.v[12] = 0.25f;
    assert(!obs_scene_native_capture(&source, 2560, 1440));
    item.draw_transform.v[12] = 0;
    capture.height = 600;
    assert(!obs_scene_native_capture(&source, 2560, 1440));
    capture.width = 3840;
    capture.height = 2160;
    item.draw_transform.v[0] = 2560.0f / 3840;
    item.draw_transform.v[5] = 1440.0f / 2160;
    assert(obs_scene_native_capture(&source, 2560, 1440) == &capture);
    capture.width = 0;
    assert(!obs_scene_native_capture(&source, 2560, 1440));
    capture.width = 3840;
    item.draw_transform.v[0] = -item.draw_transform.v[0];
    assert(!obs_scene_native_capture(&source, 2560, 1440));
    gs_texture_t texture = {GS_BGRA};
    vkcapture_source_t ctx = {.texture = &texture, .client_id = 1, .buf_id = 1};
    calldata_t cd = {0};
    vkcapture_native_texture(&ctx, &cd);
    assert(cd.texture == &texture && !cd.flip);
    ctx.tdata.flip = true;
    vkcapture_native_texture(&ctx, &cd);
    assert(cd.texture == &texture && cd.flip);
#define REJECT(field, value) do { \
    __typeof__(field) saved = field; field = value; \
    cd.texture = &texture; vkcapture_native_texture(&ctx, &cd); \
    assert(!cd.texture); field = saved; \
} while (0)
    REJECT(ctx.texture, NULL);
    REJECT(ctx.show_cursor, true);
    REJECT(ctx.allow_transparency, true);
    REJECT(ctx.tdata.color_space, HDR);
    REJECT(ctx.client_id, 0);
    REJECT(ctx.buf_id, 2);
    REJECT(client.map_memory, &client);
    REJECT(client.import_failures, IMPORT_LINEAR_HOST_MAPPED);
    REJECT(texture.format, GS_RGBA16F);
    return 0;
}
