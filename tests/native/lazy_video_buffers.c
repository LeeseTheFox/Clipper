/* Compile the packaged functions with counted allocations and injected failure. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define NUM_ENCODE_TEXTURES_MAX 10
#define GS_RENDER_TARGET 1
#define GS_SHARED_KM_TEX 2
enum { VIDEO_FORMAT_NV12, VIDEO_FORMAT_P010 };
typedef int gs_texture_t;
struct video_output_info { int format; uint32_t width, height; size_t cache_size; };
struct obs_tex_frame {
	gs_texture_t *tex, *tex_uv;
	uint32_t handle;
	uint64_t lock_key;
	bool released;
};
struct deque { size_t size; struct obs_tex_frame frames[10]; };
struct obs_core_video_mix {
	struct video_output_info *video;
	size_t gpu_encoder_texture_count;
	struct deque gpu_encoder_avail_queue;
};
static unsigned allocations, live, p010_calls;
static bool fail_uv;
static void deque_push_back(struct deque *q, const void *frame, size_t size)
{
	assert(size == sizeof(struct obs_tex_frame));
	assert(q->size / size < 10);
	memcpy(&q->frames[q->size / size], frame, size);
	q->size += size;
}
static const struct video_output_info *video_output_get_info(struct video_output_info *v) { return v; }
static bool gs_texture_create_nv12(gs_texture_t **y, gs_texture_t **uv,
				  uint32_t w, uint32_t h, unsigned flags)
{
	assert(w == 2560 && h == 1440 && flags == 3);
	*y = malloc(sizeof(**y));
	assert(*y);
	allocations++;
	live++;
	if (fail_uv) return false;
	*uv = malloc(sizeof(**uv));
	assert(*uv);
	allocations++;
	live++;
	return true;
}
static bool gs_texture_create_p010(gs_texture_t **y, gs_texture_t **uv,
				  uint32_t w, uint32_t h, unsigned flags)
{
	p010_calls++;
	return gs_texture_create_nv12(y, uv, w, h, flags);
}
static void gs_texture_destroy(gs_texture_t *tex)
{
	if (tex) { assert(live); live--; free(tex); }
}
struct video_frame { void *data; };
struct cached_frame_info { struct video_frame frame; };
struct video_output {
	struct video_output_info info;
	bool cache_initialized;
	size_t available_frames;
	struct cached_frame_info cache[16];
};
static unsigned cpu_allocations;
static void video_frame_init(struct video_frame *frame, int format, uint32_t w, uint32_t h)
{
	assert(format == VIDEO_FORMAT_NV12 && w == 2560 && h == 1440);
	frame->data = malloc(1);
	assert(frame->data);
	cpu_allocations++;
}

#include "lazy_buffers_under_test.h"

int main(void)
{
	struct video_output_info info = {VIDEO_FORMAT_NV12, 2560, 1440, 6};
	struct obs_core_video_mix mix = {.video = &info};
	/* Direct queue capacity, including growth to the limit, costs no textures. */
	for (unsigned i = 0; i < 10; i++)
		assert(grow_gpu_encoding_texture_pool(&mix));
	assert(!grow_gpu_encoding_texture_pool(&mix));
	assert(mix.gpu_encoder_texture_count == 10 && !allocations && !live);
	struct obs_tex_frame *slot = &mix.gpu_encoder_avail_queue.frames[0];
	fail_uv = true;
	assert(!ensure_gpu_encoding_texture(&mix, slot));
	assert(!slot->tex && !slot->tex_uv && !live);
	assert(mix.gpu_encoder_texture_count == 10);
	fail_uv = false;
	slot->lock_key = 42; /* Recycled direct slot must start fresh texture sync. */
	slot->released = true;
	assert(ensure_gpu_encoding_texture(&mix, slot));
	assert(live == 2 && !slot->lock_key && !slot->released);
	unsigned before = allocations;
	assert(ensure_gpu_encoding_texture(&mix, slot));
	assert(allocations == before);
	info.format = VIDEO_FORMAT_P010;
	assert(ensure_gpu_encoding_texture(&mix, &mix.gpu_encoder_avail_queue.frames[1]));
	assert(p010_calls == 1 && live == 4);
	for (unsigned i = 0; i < 10; i++) {
		gs_texture_destroy(mix.gpu_encoder_avail_queue.frames[i].tex);
		gs_texture_destroy(mix.gpu_encoder_avail_queue.frames[i].tex_uv);
	}
	assert(!live);
	struct video_output output = {.info = {VIDEO_FORMAT_NV12, 2560, 1440, 6}};
	assert(!cpu_allocations);
	init_cache(&output);
	assert(cpu_allocations == 6 && output.available_frames == 6);
	output.available_frames = 3; /* Reconnect while old frames are in flight. */
	void *first = output.cache[0].frame.data;
	init_cache(&output);
	assert(cpu_allocations == 6 && output.available_frames == 3);
	assert(output.cache[0].frame.data == first);
	for (unsigned i = 0; i < 6; i++) free(output.cache[i].frame.data);
}
