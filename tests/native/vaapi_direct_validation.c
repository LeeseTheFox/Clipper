/* Exercise actual validation code with counted GL calls and context changes. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef int graphics_t;
typedef struct { uint32_t width, height; int format; } gs_texture_t;
enum { GS_R8, GS_R8G8, GL_FRAMEBUFFER, GL_FRAMEBUFFER_COMPLETE };
struct vaapi_surface { gs_texture_t *textures[4]; uint32_t num_textures; };
struct vaapi_import {
	graphics_t *validated_context;
	uint32_t validated_width, validated_height;
};
static graphics_t contexts[2], *context = &contexts[0];
static gs_texture_t *target;
static unsigned checks, changes;
static bool complete = true;
static graphics_t *gs_get_context(void) { return context; }
static gs_texture_t *gs_get_render_target(void) { return target; }
static void gs_set_render_target(gs_texture_t *texture, void *unused)
{
	(void)unused;
	target = texture;
	changes++;
}
static uint32_t gs_texture_get_width(gs_texture_t *t) { return t->width; }
static uint32_t gs_texture_get_height(gs_texture_t *t) { return t->height; }
static int gs_texture_get_color_format(gs_texture_t *t) { return t->format; }
typedef int GLenum;
enum { GL_TIMEOUT_EXPIRED, GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED, GL_WAIT_FAILED };
struct vaapi_direct_surface { void *fence; };
static unsigned waits, timeouts;
static GLenum wait_result = GL_CONDITION_SATISFIED;
static GLenum glClientWaitSync(void *fence, int flags, uint64_t timeout)
{
	assert(fence && flags == 0 && timeout == 1000000000ULL);
	waits++;
	if (timeouts) {
		timeouts--;
		return GL_TIMEOUT_EXPIRED;
	}
	return wait_result;
}
static int glCheckFramebufferStatus(int framebuffer)
{
	assert(framebuffer == GL_FRAMEBUFFER);
	checks++;
	return complete ? GL_FRAMEBUFFER_COMPLETE : -1;
}
#include "direct_validation_under_test.h"

int main(void)
{
	gs_texture_t y = {1280, 720, GS_R8}, uv = {640, 360, GS_R8G8}, previous = {0};
	struct vaapi_surface surface = {{&y, &uv}, 2};
	struct vaapi_import entry = {0}, replacement = {0};
	target = &previous;
	assert(vaapi_direct_validate(&surface, &entry, 1280, 720));
	assert(checks == 2 && changes == 3 && target == &previous);
	for (int i = 0; i < 100; i++)
		assert(vaapi_direct_validate(&surface, &entry, 1280, 720));
	assert(checks == 2 && changes == 3);
	context = &contexts[1];
	assert(vaapi_direct_validate(&surface, &entry, 1280, 720));
	assert(checks == 4 && target == &previous);
	/* New imports/frames contexts start with fresh validation state. */
	assert(vaapi_direct_validate(&surface, &replacement, 1280, 720));
	assert(checks == 6);
	assert(!vaapi_direct_validate(&surface, &entry, 1920, 1080));
	assert(entry.validated_context == NULL && target == &previous);
	complete = false;
	assert(!vaapi_direct_validate(&surface, &entry, 1280, 720));
	assert(entry.validated_context == NULL && target == &previous);
	complete = true;
	assert(vaapi_direct_validate(&surface, &entry, 1280, 720));
	unsigned before = checks;
	/* Cache overflow uses transient imports and must validate each time. */
	assert(vaapi_direct_validate(&surface, NULL, 1280, 720));
	assert(vaapi_direct_validate(&surface, NULL, 1280, 720));
	assert(checks == before + 4 && target == &previous);
	struct vaapi_direct_surface lease = {0};
	assert(!vaapi_direct_wait(&lease) && waits == 0);
	lease.fence = &lease;
	timeouts = 2;
	assert(vaapi_direct_wait(&lease) && waits == 3);
	wait_result = GL_ALREADY_SIGNALED;
	assert(vaapi_direct_wait(&lease));
	wait_result = GL_WAIT_FAILED;
	assert(!vaapi_direct_wait(&lease));
	return 0;
}
