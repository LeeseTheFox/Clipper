/* Exercise the actual queue/lease functions extracted from the packaging patch.
 * Graphics and encoder callbacks are mocked; GPU pixels are covered separately
 * by tools/game_capture_frame_smoke.py --vaapi-direct-surfaces. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

typedef int gs_texture_t;
struct encoder_surface {
	gs_texture_t *tex[4];
	void *opaque;
	bool (*finish)(void *);
	void (*release)(void *);
};
struct obs_vframe_info { uint64_t timestamp; int count; };
struct obs_tex_frame {
	gs_texture_t *tex, *tex_uv;
	uint32_t handle;
	uint64_t timestamp, lock_key;
	int count;
	bool released;
	struct encoder_surface surface;
	int *surface_owner;
};
struct deque { size_t size; unsigned char bytes[4096]; };
static void *deque_data(struct deque *q, size_t offset) { return q->bytes + offset; }
static void deque_push_back(struct deque *q, const void *p, size_t size)
{
	assert(q->size + size <= sizeof(q->bytes));
	memcpy(q->bytes + q->size, p, size);
	q->size += size;
}
static void deque_pop_front(struct deque *q, void *p, size_t size)
{
	assert(q->size >= size);
	memcpy(p, q->bytes, size);
	q->size -= size;
	memmove(q->bytes, q->bytes + size, q->size);
}
enum { VIDEO_FORMAT_NV12, VIDEO_FORMAT_P010 };
struct video_output_info { int format; uint32_t width, height; };
typedef struct {
	struct {
		bool (*acquire_surface)(void *, uint32_t, uint32_t, struct encoder_surface *);
		bool encode_texture2;
	} info;
	struct { void *data; } context;
} obs_encoder_t;
struct obs_core_video_mix {
	bool gpu_conversion;
	struct video_output_info *video;
	struct deque vframe_info_buffer_gpu, gpu_encoder_avail_queue, gpu_encoder_queue;
	pthread_mutex_t gpu_encoder_mutex;
	struct { size_t num; obs_encoder_t **array; } gpu_encoders;
	int *gpu_encode_semaphore;
};
static unsigned renders, acquisitions, finishes, releases, refs, live;
static bool acquire_ok, finish_ok, grow_ok;
static gs_texture_t planes[2];
static int owner;
static const struct video_output_info *video_output_get_info(struct video_output_info *v) { return v; }
static void obs_weak_encoder_release(int *ref) { if (ref) { assert(refs); refs--; } }
static int *obs_encoder_get_weak_encoder(obs_encoder_t *enc) { (void)enc; refs++; return &owner; }
static void os_sem_post(int *sem) { (*sem)++; }
static void gs_set_render_target(gs_texture_t *tex, void *unused) { (void)tex; (void)unused; }
static void render_convert_texture(struct obs_core_video_mix *v, gs_texture_t **tex, gs_texture_t *input)
{
	(void)v; (void)input;
	assert(tex[0] == &planes[0] && tex[1] == &planes[1]);
	renders++;
}
static bool finish(void *data) { assert(data == &owner); finishes++; return finish_ok; }
static void release(void *data) { assert(data == &owner && live); live--; releases++; }
static bool acquire(void *data, uint32_t width, uint32_t height, struct encoder_surface *out)
{
	(void)data;
	assert(width == 1280 && height == 720);
	acquisitions++;
	if (!acquire_ok) return false;
	assert(!live);
	live++;
	*out = (struct encoder_surface){.tex = {&planes[0], &planes[1]}, .opaque = &owner,
		.finish = finish, .release = release};
	return true;
}
static bool grow_gpu_encoding_texture_pool(struct obs_core_video_mix *video)
{
	if (!grow_ok) return false;
	struct obs_tex_frame tf = {0};
	deque_push_back(&video->gpu_encoder_avail_queue, &tf, sizeof(tf));
	return true;
}

struct vaapi_import { gs_texture_t *textures[4]; uint32_t num_textures; };
struct vaapi_direct_cache {
	size_t refs;
	void *context;
	struct vaapi_import imports[32];
	size_t count;
};
static unsigned destroyed_textures, destroyed_contexts, freed_caches;
static void gs_texture_destroy(gs_texture_t *tex) { assert(tex); destroyed_textures++; }
static void av_buffer_unref(void **ref) { assert(*ref); *ref = NULL; destroyed_contexts++; }
static void bfree(void *data) { free(data); freed_caches++; }

#include "direct_queue_under_test.h"

int main(void)
{
	struct video_output_info info = {VIDEO_FORMAT_NV12, 1280, 720};
	obs_encoder_t encoder = {.info = {.acquire_surface = acquire, .encode_texture2 = true}};
	obs_encoder_t *encoders[] = {&encoder};
	int semaphore = 0;
	struct obs_core_video_mix video = {.gpu_conversion = true, .video = &info,
		.gpu_encoder_mutex = PTHREAD_MUTEX_INITIALIZER, .gpu_encoders = {1, encoders},
		.gpu_encode_semaphore = &semaphore};
	struct obs_vframe_info frame = {12345678, 1};
	assert(!render_direct_surface(&video, NULL)); /* No timing metadata. */
	deque_push_back(&video.vframe_info_buffer_gpu, &frame, sizeof(frame));
	acquire_ok = finish_ok = true;
	video.gpu_conversion = false;
	assert(!render_direct_surface(&video, NULL));
	video.gpu_conversion = true;
	info.format = VIDEO_FORMAT_P010;
	assert(!render_direct_surface(&video, NULL));
	info.format = VIDEO_FORMAT_NV12;
	video.gpu_encoders.num = 2;
	assert(!render_direct_surface(&video, NULL));
	video.gpu_encoders.num = 0;
	assert(!render_direct_surface(&video, NULL));
	video.gpu_encoders.num = 1;
	encoder.info.encode_texture2 = false;
	assert(!render_direct_surface(&video, NULL));
	encoder.info.encode_texture2 = true;
	encoder.info.acquire_surface = NULL;
	assert(!render_direct_surface(&video, NULL));
	encoder.info.acquire_surface = acquire;
	((struct obs_vframe_info *)deque_data(&video.vframe_info_buffer_gpu, 0))->count = 2;
	assert(!render_direct_surface(&video, NULL));
	((struct obs_vframe_info *)deque_data(&video.vframe_info_buffer_gpu, 0))->count = 1;
	assert(!render_direct_surface(&video, NULL)); /* Pool exhaustion. */
	assert(!acquisitions && !renders && !semaphore && !refs);
	grow_ok = true;
	acquire_ok = false;
	assert(!render_direct_surface(&video, NULL));
	assert(!live && !renders && video.vframe_info_buffer_gpu.size == sizeof(frame));
	acquire_ok = true;
	finish_ok = false;
	assert(!render_direct_surface(&video, NULL));
	assert(renders == 1 && releases == 1 && !live && !refs && !semaphore);
	assert(video.gpu_encoder_avail_queue.size == sizeof(struct obs_tex_frame));
	assert(video.vframe_info_buffer_gpu.size == sizeof(frame));
	finish_ok = true;
	assert(render_direct_surface(&video, NULL));
	assert(live == 1 && refs == 1 && semaphore == 1 && finishes == 2);
	assert(!video.gpu_encoder_avail_queue.size && !video.vframe_info_buffer_gpu.size);
	struct obs_tex_frame queued;
	deque_pop_front(&video.gpu_encoder_queue, &queued, sizeof(queued));
	assert(queued.timestamp == frame.timestamp && queued.count == 1);
	assert(queued.surface.tex[0] == &planes[0]);
	release_tex_frame_surface(&queued); /* Same cleanup on encode/skip/teardown. */
	assert(!live && !refs && releases == 2 && !queued.surface.opaque);
	release_tex_frame_surface(&queued); /* Cleared slots are safe to recycle. */
	assert(releases == 2);
	/* Encoder teardown drops its cache reference, but a queued lease must
	 * keep wrappers and their owning FFmpeg context alive until release. */
	struct vaapi_direct_cache *cache = calloc(1, sizeof(*cache));
	assert(cache);
	cache->refs = 2;
	cache->context = &owner;
	cache->count = 1;
	cache->imports[0] = (struct vaapi_import){{&planes[0], &planes[1]}, 2};
	vaapi_direct_cache_release(cache);
	assert(cache->refs == 1 && !destroyed_textures && !destroyed_contexts && !freed_caches);
	vaapi_direct_cache_release(cache);
	assert(destroyed_textures == 2 && destroyed_contexts == 1 && freed_caches == 1);
	vaapi_direct_cache_release(NULL);
	pthread_mutex_destroy(&video.gpu_encoder_mutex);
	puts("direct surface queue tests passed");
}
