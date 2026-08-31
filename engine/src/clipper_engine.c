/*
 * clipper_engine.c — Phase 2 Engine
 *
 * Long-running process that wraps libobs and exposes a Unix domain socket IPC
 * server using newline-delimited JSON. Supports display and game capture,
 * replay-buffer control, and configurable audio routing.
 *
 * Build:  make -C engine/src
 * Run:    engine/src/run_engine.sh [--socket PATH] [--output-dir DIR]
 *                                  [--max-time SECS] [--verbose]
 *
 * IPC protocol (one JSON object per line):
 *   Client→Server: {"cmd": "get_status" | "get_capabilities" |
 *                           "start_replay_buffer" | "stop_replay_buffer" |
 *                           "save_replay_buffer" | "get_preview_frame" |
 *                           "update_audio_volumes" | "shutdown"}
 *   Server→Client: {"ok": true/false, ...}   (response to command)
 *                   {"event": "clip_saved"|"status_changed", ...}  (async push)
 *
 * Startup signal (stdout, one line):
 *   READY <socket_path>
 */

#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <stdarg.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <poll.h>
#include <pthread.h>
#include <limits.h>
#include <stdint.h>
#include <wordexp.h>
#include <dirent.h>
#include <ctype.h>

#include <obs/obs.h>
#include <obs/obs-data.h>
#include <obs/obs-properties.h>
#include <obs/callback/calldata.h>
#include <obs/callback/proc.h>
#include <obs/callback/signal.h>
#include <obs/graphics/graphics.h>
#include <obs/graphics/vec2.h>
#include <obs/graphics/vec4.h>
#include <obs/obs-nix-platform.h>
#include <obs/util/base.h>
#include <gio/gio.h>
#include <glib.h>
#include <wayland-client.h>

#include "cJSON.h"

/* ── Packaged OBS paths ──────────────────────────────────────────────────── */
#define CLIPPER_PREFIX_DEFAULT "/app"
#define OBS_LIBDIR_DEFAULT CLIPPER_PREFIX_DEFAULT "/lib"
#define OBS_DATADIR_DEFAULT CLIPPER_PREFIX_DEFAULT "/share/obs"
#define VKC_PLUGIN_SO_DEFAULT \
    OBS_LIBDIR_DEFAULT "/obs-plugins/linux-vkcapture.so"
#define VKC_PLUGIN_DATA_DEFAULT \
    OBS_DATADIR_DEFAULT "/obs-plugins/linux-vkcapture"
#define PIPEWIRE_AUDIO_PLUGIN_DEFAULT \
    OBS_LIBDIR_DEFAULT "/obs-plugins/linux-pipewire-audio.so"

#define ENGINE_VERSION   "1.0.1"
#define MAX_CLIENTS      8
#define CLIENT_BUF_SIZE  4096
#define RESTORE_TOKEN_MAX 4096
#define CLIPPER_MAX_AUDIO_TRACKS 6
#define CLIPPER_MAX_AUDIO_SOURCES 24
#define AUDIO_DEVICE_ID_MAX 256
#define AUDIO_DISPLAY_NAME_MAX 256
#define AUDIO_MATCH_VALUE_MAX 256
#define CLIP_FILENAME_GAME_MAX 96
#define PREVIEW_FRAME_DEFAULT_WIDTH 360
#define PREVIEW_FRAME_DEFAULT_HEIGHT 203
#define PREVIEW_FRAME_MAX_WIDTH 640
#define PREVIEW_FRAME_MAX_HEIGHT 360

/* ── Global flags ────────────────────────────────────────────────────────── */
static volatile sig_atomic_t g_shutdown = 0;
static volatile sig_atomic_t g_restart_requested = 0;
static int                   g_verbose  = 0;
static struct wl_display    *g_obs_wayland_display = NULL;

/* ── Per-client state ────────────────────────────────────────────────────── */
typedef struct {
    int  fd;
    char buf[CLIENT_BUF_SIZE];
    int  buf_len;
    bool active;
} client_t;

typedef enum {
    CAPTURE_MODE_GAME,
    CAPTURE_MODE_DISPLAY,
} capture_mode_t;

typedef enum {
    AUDIO_MODE_SINGLE_MIX,
    AUDIO_MODE_SPLIT_TRACKS,
} audio_mode_t;

typedef enum {
    VIDEO_RATE_CONTROL_CQP,
    VIDEO_RATE_CONTROL_CBR,
    VIDEO_RATE_CONTROL_VBR,
} video_rate_control_t;

typedef enum {
    AUDIO_SOURCE_OUTPUT_DEVICE,
    AUDIO_SOURCE_INPUT_DEVICE,
    AUDIO_SOURCE_SELECTED_INPUT_DEVICE,
    AUDIO_SOURCE_APPLICATION,
    AUDIO_SOURCE_GAME_APP,
} audio_source_kind_t;

typedef struct {
    audio_source_kind_t kind;
    char backend[32];
    char device_id[AUDIO_DEVICE_ID_MAX];
    char display_name[AUDIO_DISPLAY_NAME_MAX];
    char match_type[64];
    char match_value[AUDIO_MATCH_VALUE_MAX];
} audio_source_config_t;

typedef struct {
    int track;
    bool enabled;
    float volume;
    char label[AUDIO_DISPLAY_NAME_MAX];
    size_t source_count;
    audio_source_config_t sources[CLIPPER_MAX_AUDIO_SOURCES];
} audio_track_config_t;

/* ── Engine state ────────────────────────────────────────────────────────── */
typedef struct {
    /* libobs objects — created once, started/stopped on demand */
    obs_output_t  *replay_output;
    obs_encoder_t *venc;
    obs_encoder_t *aenc[CLIPPER_MAX_AUDIO_TRACKS];
    bool           buffer_active;

    /* Reused low-resolution preview readback resources. */
    gs_texrender_t *preview_texrender;
    gs_stagesurf_t *preview_stagesurf;
    uint8_t        *preview_frame;
    size_t          preview_frame_size;
    uint32_t        preview_width;
    uint32_t        preview_height;

    /* vkcapture scene and source */
    obs_scene_t   *scene;
    obs_source_t  *vkc_source;
    
    /* Audio sources */
    obs_source_t  *audio_sources[CLIPPER_MAX_AUDIO_SOURCES];
    int            audio_source_tracks[CLIPPER_MAX_AUDIO_SOURCES];
    audio_source_kind_t audio_source_kinds[CLIPPER_MAX_AUDIO_SOURCES];
    size_t         audio_source_count;
    
    /* Display source: PipeWire portal on Wayland, XSHM on X11. */
    obs_source_t  *display_source;
    GDBusConnection *display_portal_bus;
    guint display_portal_response_subscription_id;
    bool display_portal_cancelled;

    /* Hook state tracking */
    struct timespec last_hook_check;
    bool            game_hooked;
    char            game_exe[256];
    bool            logged_missing_hook_proc;
    bool            logged_hook_poll;

    /* Configuration */
    char output_dir[PATH_MAX];
    int  max_time_sec;
    int  max_size_mb;
    int  fps;
    int  base_width;
    int  base_height;
    int  output_width;
    int  output_height;
    char output_format[32];
    char video_encoder[64];
    char audio_encoder[64];
    video_rate_control_t video_rate_control;
    int  quality_cqp;
    int  video_bitrate;
    int  video_max_bitrate;
    int  audio_bitrate;
    char vaapi_device[PATH_MAX];
    capture_mode_t capture_mode;
    char pipewire_restore_token[RESTORE_TOKEN_MAX];
    struct timespec last_restore_token_check;
    audio_mode_t audio_mode;
    bool mic_enabled;
    char mic_backend[32];
    char mic_device_id[AUDIO_DEVICE_ID_MAX];
    char mic_display_name[AUDIO_DISPLAY_NAME_MAX];
    float mic_volume;
    audio_track_config_t audio_tracks[CLIPPER_MAX_AUDIO_TRACKS];

    /* IPC */
    int  server_fd;
    char socket_path[PATH_MAX];

    /*
     * Client table.
     *
     * Locking discipline:
     *   clients_mutex protects:
     *     - client.active  (read by bg thread, written by main thread)
     *     - client.fd      (read by bg thread to write events; written by
     *                       main thread in accept/close)
     *     - all writes to any client fd (prevents interleaving with
     *       background-thread event broadcasts)
     *
     *   client.buf / client.buf_len are accessed only by the main thread
     *   (reads from client fds happen only in the event loop); no mutex needed.
     */
    client_t        clients[MAX_CLIENTS];
    pthread_mutex_t clients_mutex;
} engine_state_t;

/* ── Signal handler ──────────────────────────────────────────────────────── */
static void handle_signal(int sig)
{
    (void)sig;
    g_shutdown = 1;
}

/* ── libobs log handler ──────────────────────────────────────────────────── */
static void engine_log(int log_level, const char *msg, va_list args, void *p)
{
    (void)p;
    if (log_level <= LOG_WARNING || g_verbose) {
        fprintf(stderr, "[engine] ");
        vfprintf(stderr, msg, args);
        fputc('\n', stderr);
    }
}

/* ── mkdir -p equivalent ─────────────────────────────────────────────────── */
static int mkdirs(const char *path)
{
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof(tmp), "%s", path);

    size_t len = strlen(tmp);
    if (len > 0 && tmp[len - 1] == '/')
        tmp[--len] = '\0';

    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(tmp, 0755) < 0 && errno != EEXIST)
                return -1;
            *p = '/';
        }
    }
    if (mkdir(tmp, 0755) < 0 && errno != EEXIST)
        return -1;
    return 0;
}

static void copy_string(char *dst, size_t dst_size, const char *src)
{
    if (!dst || dst_size == 0)
        return;

    if (!src)
        src = "";

    snprintf(dst, dst_size, "%s", src);
}

static bool is_safe_clip_name_char(unsigned char ch)
{
    return isalnum(ch) || ch == '-' || ch == '_' || ch == '.';
}

static void sanitize_clip_filename_part(const char *input, char *output, size_t size)
{
    if (!output || size == 0)
        return;

    output[0] = '\0';
    if (!input)
        return;

    size_t out = 0;
    bool pending_separator = false;
    for (const unsigned char *p = (const unsigned char *)input;
         *p && out + 1 < size; p++) {
        if (is_safe_clip_name_char(*p)) {
            output[out++] = (char)*p;
            pending_separator = false;
        } else if (!pending_separator && out > 0) {
            output[out++] = '_';
            pending_separator = true;
        }
    }

    while (out > 0 && output[out - 1] == '_')
        out--;
    output[out] = '\0';
}

/* ── Default socket path ─────────────────────────────────────────────────── */
static void get_default_socket_path(char *buf, size_t size)
{
    const char *xdg = getenv("XDG_RUNTIME_DIR");
    if (xdg && *xdg)
        snprintf(buf, size, "%s/clipper-engine.sock", xdg);
    else
        snprintf(buf, size, "/tmp/clipper-engine-%u.sock", (unsigned)getuid());
}

/* ── JSON write helpers ──────────────────────────────────────────────────── */

/*
 * Write a JSON object + newline to fd.
 * Caller MUST hold state->clients_mutex when writing to a client fd so that
 * main-thread responses and background-thread event broadcasts don't interleave.
 */
static void write_json_locked(int fd, cJSON *obj)
{
    char *str = cJSON_PrintUnformatted(obj);
    if (!str) return;

    size_t len = strlen(str);
    size_t done = 0;
    while (done < len) {
        ssize_t n = write(fd, str + done, len - done);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                struct pollfd pfd = {
                    .fd = fd,
                    .events = POLLOUT,
                    .revents = 0,
                };
                int ret = poll(&pfd, 1, 1000);
                if (ret > 0 && (pfd.revents & POLLOUT))
                    continue;
            }
            free(str);
            return;
        }
        if (n == 0) {
            free(str);
            return;
        }
        done += (size_t)n;
    }

    for (;;) {
        ssize_t n = write(fd, "\n", 1);
        if (n == 1)
            break;
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd pfd = {
                .fd = fd,
                .events = POLLOUT,
                .revents = 0,
            };
            int ret = poll(&pfd, 1, 1000);
            if (ret > 0 && (pfd.revents & POLLOUT))
                continue;
        }
        break;
    }

    free(str);
}

/* Send to a single client fd, acquiring the mutex. */
static void send_to_client(engine_state_t *state, int fd, cJSON *obj)
{
    pthread_mutex_lock(&state->clients_mutex);
    write_json_locked(fd, obj);
    pthread_mutex_unlock(&state->clients_mutex);
}

/*
 * Broadcast an event to ALL connected clients.
 * Safe to call from any thread; acquires the mutex internally.
 */
static void broadcast_event(engine_state_t *state, cJSON *event)
{
    pthread_mutex_lock(&state->clients_mutex);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (state->clients[i].active)
            write_json_locked(state->clients[i].fd, event);
    }
    pthread_mutex_unlock(&state->clients_mutex);
}

static void on_display_portal_response(GDBusConnection *connection,
                                       const gchar *sender_name,
                                       const gchar *object_path,
                                       const gchar *interface_name,
                                       const gchar *signal_name,
                                       GVariant *parameters,
                                       gpointer user_data)
{
    (void)connection;
    (void)sender_name;
    (void)object_path;
    (void)interface_name;
    (void)signal_name;

    engine_state_t *state = user_data;
    guint response = 2;
    GVariant *results = NULL;
    g_variant_get(parameters, "(u@a{sv})", &response, &results);
    if (results)
        g_variant_unref(results);

    if (response == 0 || state->display_portal_cancelled)
        return;

    state->display_portal_cancelled = true;
    fprintf(stderr, "[engine] Display picker was cancelled (portal response %u)\n",
            response);

    cJSON *event = cJSON_CreateObject();
    if (!event)
        return;
    cJSON_AddStringToObject(event, "event", "display_target_cancelled");
    cJSON_AddNumberToObject(event, "portal_response", response);
    broadcast_event(state, event);
    cJSON_Delete(event);
}

static void subscribe_display_portal_response(engine_state_t *state)
{
    if (state->display_portal_response_subscription_id != 0)
        return;

    GError *error = NULL;
    state->display_portal_bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (!state->display_portal_bus) {
        fprintf(stderr, "[engine] WARN: Could not monitor display picker: %s\n",
                error ? error->message : "unknown D-Bus error");
        g_clear_error(&error);
        return;
    }

    state->display_portal_response_subscription_id =
        g_dbus_connection_signal_subscribe(
            state->display_portal_bus,
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            NULL,
            NULL,
            G_DBUS_SIGNAL_FLAGS_NONE,
            on_display_portal_response,
            state,
            NULL);
}

static void unsubscribe_display_portal_response(engine_state_t *state)
{
    if (state->display_portal_bus &&
        state->display_portal_response_subscription_id != 0) {
        g_dbus_connection_signal_unsubscribe(
            state->display_portal_bus,
            state->display_portal_response_subscription_id);
    }
    state->display_portal_response_subscription_id = 0;
    g_clear_object(&state->display_portal_bus);
}

/* ── Client management ───────────────────────────────────────────────────── */
static void close_client(engine_state_t *state, int idx)
{
    pthread_mutex_lock(&state->clients_mutex);
    if (state->clients[idx].active) {
        close(state->clients[idx].fd);
        state->clients[idx].fd      = -1;
        state->clients[idx].active  = false;
        state->clients[idx].buf_len = 0;
    }
    pthread_mutex_unlock(&state->clients_mutex);
}

static void accept_client(engine_state_t *state)
{
    struct sockaddr_un addr;
    socklen_t addr_len = sizeof(addr);
    int cfd = accept(state->server_fd, (struct sockaddr *)&addr, &addr_len);
    if (cfd < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK)
            perror("[engine] accept");
        return;
    }

    /* Set non-blocking + close-on-exec */
    int fl = fcntl(cfd, F_GETFL, 0);
    if (fl >= 0) fcntl(cfd, F_SETFL, fl | O_NONBLOCK);
    int fd_fl = fcntl(cfd, F_GETFD, 0);
    if (fd_fl >= 0) fcntl(cfd, F_SETFD, fd_fl | FD_CLOEXEC);

    pthread_mutex_lock(&state->clients_mutex);
    int slot = -1;
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (!state->clients[i].active) { slot = i; break; }
    }
    if (slot < 0) {
        pthread_mutex_unlock(&state->clients_mutex);
        fprintf(stderr, "[engine] max clients (%d) reached, dropping connection\n",
                MAX_CLIENTS);
        close(cfd);
        return;
    }
    state->clients[slot].fd      = cfd;
    state->clients[slot].buf_len = 0;
    state->clients[slot].active  = true;
    pthread_mutex_unlock(&state->clients_mutex);

    if (g_verbose)
        fprintf(stderr, "[engine] client connected (slot %d, fd %d)\n", slot, cfd);
}

/* ── "saved" signal callback (fired from a libobs background thread) ─────── */
static void on_clip_saved(void *param, calldata_t *cd)
{
    (void)cd;   /* signal calldata doesn't carry the path; use get_last_replay */
    engine_state_t *state = (engine_state_t *)param;

    /* Retrieve the path via the proc handler "get_last_replay" (key: "path") */
    const char *path = "";
    calldata_t replay_cd = {0};
    proc_handler_t *ph = obs_output_get_proc_handler(state->replay_output);
    if (ph && proc_handler_call(ph, "get_last_replay", &replay_cd)) {
        const char *p = calldata_string(&replay_cd, "path");
        if (p && *p) path = p;
    }

    cJSON *ev = cJSON_CreateObject();
    if (!ev) {
        calldata_free(&replay_cd);
        return;
    }
    cJSON_AddStringToObject(ev, "event", "clip_saved");
    cJSON_AddStringToObject(ev, "path",  path);

    broadcast_event(state, ev);      /* acquires mutex internally */
    cJSON_Delete(ev);
    calldata_free(&replay_cd);
}

/* ── Command handlers ────────────────────────────────────────────────────── */

static void send_error(engine_state_t *state, int fd, const char *msg)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 0);
    cJSON_AddStringToObject(r, "error", msg);
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static const char *capture_mode_to_string(capture_mode_t mode)
{
    switch (mode) {
    case CAPTURE_MODE_DISPLAY:
        return "display_capture";
    case CAPTURE_MODE_GAME:
    default:
        return "game_capture";
    }
}

static bool set_capture_mode_from_string(engine_state_t *state, const char *value)
{
    if (!value)
        return false;

    if (strcmp(value, "display_capture") == 0) {
        state->capture_mode = CAPTURE_MODE_DISPLAY;
        return true;
    }
    if (strcmp(value, "game_capture") == 0) {
        state->capture_mode = CAPTURE_MODE_GAME;
        return true;
    }

    return false;
}

static const char *video_rate_control_to_config_string(video_rate_control_t mode)
{
    switch (mode) {
    case VIDEO_RATE_CONTROL_CBR:
        return "cbr";
    case VIDEO_RATE_CONTROL_VBR:
        return "vbr";
    case VIDEO_RATE_CONTROL_CQP:
    default:
        return "cqp";
    }
}

static bool set_video_rate_control_from_string(engine_state_t *state, const char *value)
{
    if (!value)
        return false;

    if (strcmp(value, "cqp") == 0 || strcmp(value, "CQP") == 0 ||
        strcmp(value, "crf") == 0 || strcmp(value, "CRF") == 0) {
        state->video_rate_control = VIDEO_RATE_CONTROL_CQP;
        return true;
    }
    if (strcmp(value, "cbr") == 0 || strcmp(value, "CBR") == 0) {
        state->video_rate_control = VIDEO_RATE_CONTROL_CBR;
        return true;
    }
    if (strcmp(value, "vbr") == 0 || strcmp(value, "VBR") == 0) {
        state->video_rate_control = VIDEO_RATE_CONTROL_VBR;
        return true;
    }

    return false;
}

static int legacy_quality_to_cqp(const char *quality, int fallback)
{
    if (!quality)
        return fallback;
    if (strcmp(quality, "high") == 0)
        return 18;
    if (strcmp(quality, "low") == 0)
        return 28;
    return 23;
}

static uint32_t track_mask(int track)
{
    if (track < 1 || track > CLIPPER_MAX_AUDIO_TRACKS)
        return 0;

    return 1u << (track - 1);
}

static float clamp_audio_volume(double value)
{
    if (value < 0.0)
        return 0.0f;
    if (value > 2.0)
        return 2.0f;
    return (float)value;
}

static const char *audio_mode_to_string(audio_mode_t mode)
{
    return mode == AUDIO_MODE_SPLIT_TRACKS ? "split_tracks" : "single_mix";
}

static bool set_audio_mode_from_string(engine_state_t *state, const char *value)
{
    if (!value)
        return false;
    if (strcmp(value, "single_mix") == 0) {
        state->audio_mode = AUDIO_MODE_SINGLE_MIX;
        return true;
    }
    if (strcmp(value, "split_tracks") == 0) {
        state->audio_mode = AUDIO_MODE_SPLIT_TRACKS;
        return true;
    }
    return false;
}

static const char *audio_source_kind_to_string(audio_source_kind_t kind)
{
    switch (kind) {
    case AUDIO_SOURCE_SELECTED_INPUT_DEVICE:
        return "selected_input_device";
    case AUDIO_SOURCE_INPUT_DEVICE:
        return "input_device";
    case AUDIO_SOURCE_APPLICATION:
        return "application";
    case AUDIO_SOURCE_GAME_APP:
        return "game_app";
    case AUDIO_SOURCE_OUTPUT_DEVICE:
    default:
        return "output_device";
    }
}

static bool audio_source_kind_from_string(const char *value, audio_source_kind_t *kind)
{
    if (!value || !kind)
        return false;

    if (strcmp(value, "output_device") == 0) {
        *kind = AUDIO_SOURCE_OUTPUT_DEVICE;
        return true;
    }
    if (strcmp(value, "input_device") == 0) {
        *kind = AUDIO_SOURCE_INPUT_DEVICE;
        return true;
    }
    if (strcmp(value, "selected_input_device") == 0) {
        *kind = AUDIO_SOURCE_SELECTED_INPUT_DEVICE;
        return true;
    }
    if (strcmp(value, "application") == 0) {
        *kind = AUDIO_SOURCE_APPLICATION;
        return true;
    }
    if (strcmp(value, "game_app") == 0) {
        *kind = AUDIO_SOURCE_GAME_APP;
        return true;
    }

    return false;
}

static void broadcast_status_changed(engine_state_t *state, const char *status)
{
    cJSON *ev = cJSON_CreateObject();
    if (!ev)
        return;

    cJSON_AddStringToObject(ev, "event", "status_changed");
    cJSON_AddStringToObject(ev, "status", status);
    broadcast_event(state, ev);
    cJSON_Delete(ev);
}

static bool start_replay_buffer_internal(engine_state_t *state, bool emit_event)
{
    if (state->buffer_active)
        return true;

    if (!state->replay_output || !obs_output_start(state->replay_output))
        return false;

    state->buffer_active = true;
    if (emit_event)
        broadcast_status_changed(state, "active");

    return true;
}

static void handle_get_status(engine_state_t *state, int fd)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddStringToObject(r, "version", ENGINE_VERSION);
    cJSON_AddStringToObject(r, "status", state->buffer_active ? "active" : "idle");
    cJSON_AddStringToObject(r, "capture_mode", capture_mode_to_string(state->capture_mode));
    cJSON_AddBoolToObject(
        r, "display_target_selected",
        state->capture_mode == CAPTURE_MODE_DISPLAY &&
            state->pipewire_restore_token[0] != '\0');
    cJSON_AddBoolToObject(r, "display_target_cancelled",
                          state->display_portal_cancelled);
    cJSON_AddStringToObject(r, "audio_mode", audio_mode_to_string(state->audio_mode));
    cJSON_AddNumberToObject(r, "fps", state->fps);
    cJSON_AddNumberToObject(r, "base_width", state->base_width);
    cJSON_AddNumberToObject(r, "base_height", state->base_height);
    cJSON_AddNumberToObject(r, "output_width", state->output_width);
    cJSON_AddNumberToObject(r, "output_height", state->output_height);
    cJSON_AddStringToObject(r, "video_encoder", state->video_encoder);
    cJSON_AddStringToObject(r, "rate_control",
                            video_rate_control_to_config_string(state->video_rate_control));
    cJSON_AddNumberToObject(r, "quality_cqp", state->quality_cqp);
    cJSON_AddNumberToObject(r, "video_bitrate", state->video_bitrate);
    cJSON_AddNumberToObject(r, "video_max_bitrate", state->video_max_bitrate);
    cJSON_AddNumberToObject(r, "replay_buffer_size_mb", state->max_size_mb);
    cJSON_AddBoolToObject(r, "buffer_active", state->buffer_active);
    cJSON_AddBoolToObject(r, "game_hooked", state->game_hooked);
    
    if (state->game_hooked && state->game_exe[0] != '\0') {
        cJSON_AddStringToObject(r, "game_exe", state->game_exe);
    } else {
        cJSON_AddNullToObject(r, "game_exe");
    }
    
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static int preview_dimension_from_json(cJSON *json, const char *key,
                                       int fallback, int max_value)
{
    cJSON *item = cJSON_GetObjectItem(json, key);
    int value = fallback;
    if (item && cJSON_IsNumber(item))
        value = item->valueint;

    if (value < 1)
        value = fallback;
    if (value > max_value)
        value = max_value;
    return value;
}

static bool preview_bool_from_json(cJSON *json, const char *key, bool fallback)
{
    cJSON *item = cJSON_GetObjectItem(json, key);
    if (!item || !cJSON_IsBool(item))
        return fallback;
    return cJSON_IsTrue(item);
}

static void fit_preview_dimensions(uint32_t source_width, uint32_t source_height,
                                   int max_width, int max_height,
                                   int *width_out, int *height_out)
{
    int width = max_width;
    int height = max_height;

    if (source_width > 0 && source_height > 0 && max_width > 0 && max_height > 0) {
        uint64_t source_w = source_width;
        uint64_t source_h = source_height;
        uint64_t box_w = (uint64_t)max_width;
        uint64_t box_h = (uint64_t)max_height;

        if (source_w * box_h >= source_h * box_w) {
            uint64_t fitted_height = (source_h * box_w + source_w / 2) / source_w;
            width = max_width;
            height = (int)fitted_height;
        } else {
            uint64_t fitted_width = (source_w * box_h + source_h / 2) / source_h;
            width = (int)fitted_width;
            height = max_height;
        }
    }

    if (width < 1)
        width = 1;
    if (height < 1)
        height = 1;
    if (width > max_width)
        width = max_width;
    if (height > max_height)
        height = max_height;

    *width_out = width;
    *height_out = height;
}

static void destroy_preview_resources_locked(engine_state_t *state)
{
    if (state->preview_stagesurf) {
        gs_stagesurface_destroy(state->preview_stagesurf);
        state->preview_stagesurf = NULL;
    }
    if (state->preview_texrender) {
        gs_texrender_destroy(state->preview_texrender);
        state->preview_texrender = NULL;
    }
    free(state->preview_frame);
    state->preview_frame = NULL;
    state->preview_frame_size = 0;
    state->preview_width = 0;
    state->preview_height = 0;
}

static bool ensure_preview_resources(engine_state_t *state,
                                     uint32_t width, uint32_t height,
                                     bool *resources_reused)
{
    size_t frame_size = (size_t)width * height * 4;
    bool reusable = state->preview_texrender && state->preview_stagesurf &&
                    state->preview_frame && state->preview_width == width &&
                    state->preview_height == height &&
                    state->preview_frame_size == frame_size;
    if (reusable) {
        *resources_reused = true;
        return true;
    }

    destroy_preview_resources_locked(state);
    state->preview_texrender = gs_texrender_create(GS_BGRA, GS_ZS_NONE);
    state->preview_stagesurf = gs_stagesurface_create(width, height, GS_BGRA);
    state->preview_frame = malloc(frame_size);
    if (!state->preview_texrender || !state->preview_stagesurf ||
        !state->preview_frame) {
        destroy_preview_resources_locked(state);
        return false;
    }

    state->preview_frame_size = frame_size;
    state->preview_width = width;
    state->preview_height = height;
    *resources_reused = false;
    return true;
}

static bool capture_preview_frame(engine_state_t *state, int max_width, int max_height,
                                  bool preserve_aspect, int *width_out, int *height_out,
                                  uint8_t **frame_out, size_t *frame_size_out,
                                  uint32_t *stride_out, bool *resources_reused_out)
{
    if (!width_out || !height_out || !frame_out || !frame_size_out ||
        !stride_out || !resources_reused_out || max_width <= 0 || max_height <= 0)
        return false;

    *width_out = 0;
    *height_out = 0;
    *frame_out = NULL;
    *frame_size_out = 0;
    *stride_out = 0;
    *resources_reused_out = false;

    bool ok = false;
    size_t frame_size = 0;
    uint32_t stride = 0;

    obs_enter_graphics();

    gs_texture_t *main_texture = obs_get_main_texture();
    gs_effect_t *effect = obs_get_base_effect(OBS_EFFECT_BILINEAR_LOWRES);
    if (!main_texture)
        goto done;
    if (!effect)
        effect = obs_get_base_effect(OBS_EFFECT_DEFAULT);
    if (!effect)
        goto done;

    int width = max_width;
    int height = max_height;
    if (preserve_aspect) {
        fit_preview_dimensions(
            gs_texture_get_width(main_texture),
            gs_texture_get_height(main_texture),
            max_width,
            max_height,
            &width,
            &height
        );
    }

    uint32_t frame_width = (uint32_t)width;
    uint32_t frame_height = (uint32_t)height;
    stride = frame_width * 4;
    frame_size = (size_t)stride * frame_height;
    if (!ensure_preview_resources(
            state, frame_width, frame_height, resources_reused_out))
        goto done;

    gs_texrender_reset(state->preview_texrender);
    if (!gs_texrender_begin(state->preview_texrender, frame_width, frame_height))
        goto done;

    gs_viewport_push();
    gs_projection_push();
    gs_matrix_push();
    gs_blend_state_push();

    struct vec4 clear_color = {0};
    gs_set_viewport(0, 0, (int)frame_width, (int)frame_height);
    gs_ortho(0.0f, (float)frame_width, 0.0f, (float)frame_height, -100.0f, 100.0f);
    gs_matrix_identity();
    gs_reset_blend_state();
    gs_enable_depth_test(false);
    gs_clear(GS_CLEAR_COLOR, &clear_color, 0.0f, 0);
    while (gs_effect_loop(effect, "Draw"))
        obs_source_draw(main_texture, 0, 0, frame_width, frame_height, false);

    gs_blend_state_pop();
    gs_matrix_pop();
    gs_projection_pop();
    gs_viewport_pop();
    gs_texrender_end(state->preview_texrender);

    gs_texture_t *rendered_texture =
        gs_texrender_get_texture(state->preview_texrender);
    if (!rendered_texture)
        goto done;

    gs_stage_texture(state->preview_stagesurf, rendered_texture);
    gs_flush();

    uint8_t *mapped = NULL;
    uint32_t mapped_stride = 0;
    if (!gs_stagesurface_map(state->preview_stagesurf, &mapped, &mapped_stride))
        goto done;

    for (uint32_t y = 0; y < frame_height; y++)
        memcpy(state->preview_frame + ((size_t)y * stride),
               mapped + ((size_t)y * mapped_stride), stride);

    gs_stagesurface_unmap(state->preview_stagesurf);
    ok = true;

done:
    obs_leave_graphics();

    if (!ok)
        return false;

    *frame_out = state->preview_frame;
    *frame_size_out = frame_size;
    *stride_out = stride;
    *width_out = (int)frame_width;
    *height_out = (int)frame_height;
    return true;
}

static void handle_get_preview_frame(engine_state_t *state, int fd, cJSON *json)
{
    if (!state->buffer_active) {
        send_error(state, fd, "preview unavailable while replay buffer is inactive");
        return;
    }

    int width = preview_dimension_from_json(
        json, "width", PREVIEW_FRAME_DEFAULT_WIDTH, PREVIEW_FRAME_MAX_WIDTH);
    int height = preview_dimension_from_json(
        json, "height", PREVIEW_FRAME_DEFAULT_HEIGHT, PREVIEW_FRAME_MAX_HEIGHT);
    bool preserve_aspect = preview_bool_from_json(json, "preserve_aspect", false);

    uint8_t *frame = NULL;
    size_t frame_size = 0;
    uint32_t stride = 0;
    bool resources_reused = false;
    if (!capture_preview_frame(state, width, height, preserve_aspect, &width, &height,
                               &frame, &frame_size, &stride, &resources_reused)) {
        send_error(state, fd, "preview frame unavailable");
        return;
    }

    gchar *encoded = g_base64_encode(frame, frame_size);

    if (!encoded) {
        send_error(state, fd, "failed to encode preview frame");
        return;
    }

    cJSON *r = cJSON_CreateObject();
    if (!r) {
        g_free(encoded);
        send_error(state, fd, "failed to allocate preview response");
        return;
    }

    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddNumberToObject(r, "width", width);
    cJSON_AddNumberToObject(r, "height", height);
    cJSON_AddNumberToObject(r, "stride", (double)stride);
    cJSON_AddStringToObject(r, "format", "BGRA");
    cJSON_AddStringToObject(r, "data", encoded);
    cJSON_AddBoolToObject(r, "resources_reused", resources_reused);

    send_to_client(state, fd, r);
    cJSON_Delete(r);
    g_free(encoded);
}

static bool string_contains_ci(const char *haystack, const char *needle)
{
    if (!haystack || !needle || !*needle)
        return false;

    size_t needle_len = strlen(needle);
    for (const char *p = haystack; *p; p++) {
        size_t i = 0;
        while (i < needle_len && p[i] &&
               tolower((unsigned char)p[i]) == tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == needle_len)
            return true;
    }

    return false;
}

static bool encoder_is_hardware(const char *id, const char *name)
{
    static const char *const markers[] = {
        "vaapi", "nvenc", "qsv", "amf", "videotoolbox", "apple", "v4l2"
    };

    for (size_t i = 0; i < sizeof(markers) / sizeof(markers[0]); i++) {
        if (string_contains_ci(id, markers[i]) || string_contains_ci(name, markers[i]))
            return true;
    }

    return string_contains_ci(name, "hardware");
}

static void add_encoder_capability(cJSON *array, const char *id)
{
    if (!array || !id)
        return;

    uint32_t caps = obs_get_encoder_caps(id);
    if (caps & (OBS_ENCODER_CAP_DEPRECATED | OBS_ENCODER_CAP_INTERNAL))
        return;

    const char *name = obs_encoder_get_display_name(id);
    const char *codec = obs_get_encoder_codec(id);

    cJSON *encoder = cJSON_CreateObject();
    if (!encoder)
        return;

    cJSON_AddStringToObject(encoder, "id", id);
    cJSON_AddStringToObject(encoder, "name", name && *name ? name : id);
    cJSON_AddStringToObject(encoder, "codec", codec && *codec ? codec : "");
    cJSON_AddBoolToObject(encoder, "hardware", encoder_is_hardware(id, name));
    cJSON_AddItemToArray(array, encoder);
}

static void add_encoder_capabilities(cJSON *video_encoders, cJSON *audio_encoders)
{
    const char *id = NULL;

    for (size_t idx = 0; obs_enum_encoder_types(idx, &id); idx++) {
        enum obs_encoder_type type = obs_get_encoder_type(id);
        if (type == OBS_ENCODER_VIDEO)
            add_encoder_capability(video_encoders, id);
        else if (type == OBS_ENCODER_AUDIO)
            add_encoder_capability(audio_encoders, id);
    }
}

static bool json_string_array_contains(cJSON *array, const char *value)
{
    cJSON *item = NULL;
    cJSON_ArrayForEach(item, array) {
        if (cJSON_IsString(item) && strcmp(item->valuestring, value) == 0)
            return true;
    }
    return false;
}

static void add_unique_string(cJSON *array, const char *value)
{
    if (!array || !value || !*value || json_string_array_contains(array, value))
        return;

    cJSON *item = cJSON_CreateString(value);
    if (item)
        cJSON_AddItemToArray(array, item);
}

static bool is_supported_output_format(const char *format)
{
    static const char *const supported_formats[] = {
        "mkv", "mp4", "mov", "ts"
    };

    if (!format)
        return false;

    for (size_t i = 0; i < sizeof(supported_formats) / sizeof(supported_formats[0]); i++) {
        if (strcmp(format, supported_formats[i]) == 0)
            return true;
    }

    return false;
}

static void add_format_fallbacks(cJSON *formats)
{
    static const char *const fallback_formats[] = {
        "mkv", "mp4", "mov", "ts"
    };

    for (size_t i = 0; i < sizeof(fallback_formats) / sizeof(fallback_formats[0]); i++)
        add_unique_string(formats, fallback_formats[i]);
}

static void add_formats_from_output_properties(cJSON *formats)
{
    obs_properties_t *props = obs_get_output_properties("replay_buffer");
    if (!props) {
        add_format_fallbacks(formats);
        return;
    }

    for (obs_property_t *prop = obs_properties_first(props); prop;
         obs_property_next(&prop)) {
        enum obs_property_type type = obs_property_get_type(prop);
        enum obs_combo_format format = OBS_COMBO_FORMAT_INVALID;

        if (type != OBS_PROPERTY_LIST)
            continue;

        format = obs_property_list_format(prop);
        if (format != OBS_COMBO_FORMAT_STRING)
            continue;

        const char *prop_name = obs_property_name(prop);
        if (prop_name &&
            strcmp(prop_name, "extension") != 0 &&
            strcmp(prop_name, "format") != 0 &&
            strcmp(prop_name, "muxer_settings") != 0)
            continue;

        size_t count = obs_property_list_item_count(prop);
        for (size_t i = 0; i < count; i++) {
            const char *value = obs_property_list_item_string(prop, i);
            if (is_supported_output_format(value))
                add_unique_string(formats, value);
        }
    }

    if (cJSON_GetArraySize(formats) == 0)
        add_format_fallbacks(formats);

    obs_properties_destroy(props);
}

static void add_vaapi_devices(cJSON *devices)
{
    cJSON *automatic = cJSON_CreateObject();
    if (automatic) {
        cJSON_AddStringToObject(automatic, "id", "auto");
        cJSON_AddStringToObject(automatic, "name", "Automatic");
        cJSON_AddItemToArray(devices, automatic);
    }

    DIR *dir = opendir("/dev/dri");
    if (!dir)
        return;

    struct dirent *entry = NULL;
    while ((entry = readdir(dir)) != NULL) {
        if (strncmp(entry->d_name, "renderD", 7) != 0)
            continue;

        char path[PATH_MAX];
        snprintf(path, sizeof(path), "/dev/dri/%s", entry->d_name);

        cJSON *device = cJSON_CreateObject();
        if (!device)
            continue;
        cJSON_AddStringToObject(device, "id", path);
        cJSON_AddStringToObject(device, "name", path);
        cJSON_AddItemToArray(devices, device);
    }

    closedir(dir);
}

static bool encoder_array_has_id_fragment(cJSON *encoders, const char *fragment)
{
    cJSON *encoder = NULL;
    cJSON_ArrayForEach(encoder, encoders) {
        cJSON *id = cJSON_GetObjectItemCaseSensitive(encoder, "id");
        if (id && cJSON_IsString(id) && id->valuestring &&
            strstr(id->valuestring, fragment))
            return true;
    }
    return false;
}

static void handle_get_capabilities(engine_state_t *state, int fd)
{
    cJSON *r = cJSON_CreateObject();
    cJSON *video_encoders = cJSON_CreateArray();
    cJSON *audio_encoders = cJSON_CreateArray();
    cJSON *formats = cJSON_CreateArray();
    cJSON *vaapi_devices = cJSON_CreateArray();
    cJSON *capture_modes = cJSON_CreateArray();

    if (!r || !video_encoders || !audio_encoders || !formats || !vaapi_devices ||
        !capture_modes) {
        cJSON_Delete(r);
        cJSON_Delete(video_encoders);
        cJSON_Delete(audio_encoders);
        cJSON_Delete(formats);
        cJSON_Delete(vaapi_devices);
        cJSON_Delete(capture_modes);
        send_error(state, fd, "failed to allocate capabilities response");
        return;
    }

    add_encoder_capabilities(video_encoders, audio_encoders);
    add_formats_from_output_properties(formats);
    add_vaapi_devices(vaapi_devices);
    add_unique_string(capture_modes, "game_capture");
    bool display_capture_available =
        obs_source_get_display_name("pipewire-screen-capture-source") != NULL ||
        obs_source_get_display_name("xshm_input") != NULL;
    if (display_capture_available)
        add_unique_string(capture_modes, "display_capture");

    int vaapi_device_count = cJSON_GetArraySize(vaapi_devices) - 1;
    if (vaapi_device_count < 0)
        vaapi_device_count = 0;
    fprintf(stderr,
            "[engine] OBS settings probe detected: %d video encoders, "
            "%d audio encoders; VAAPI encoder: %s; VAAPI render devices: %d; "
            "display capture: %s\n",
            cJSON_GetArraySize(video_encoders),
            cJSON_GetArraySize(audio_encoders),
            encoder_array_has_id_fragment(video_encoders, "vaapi") ? "yes" : "no",
            vaapi_device_count,
            display_capture_available ? "available" : "unavailable");

    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddItemToObject(r, "video_encoders", video_encoders);
    cJSON_AddItemToObject(r, "audio_encoders", audio_encoders);
    cJSON_AddItemToObject(r, "formats", formats);
    cJSON_AddItemToObject(r, "vaapi_devices", vaapi_devices);
    cJSON_AddItemToObject(r, "capture_modes", capture_modes);

    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void add_audio_source_id_if_registered(cJSON *array, const char *source_id)
{
    if (obs_source_get_display_name(source_id))
        add_unique_string(array, source_id);
}

static void handle_get_audio_capabilities(engine_state_t *state, int fd)
{
    (void)state;

    cJSON *r = cJSON_CreateObject();
    cJSON *source_ids = cJSON_CreateArray();
    cJSON *backends = cJSON_CreateArray();
    if (!r || !source_ids || !backends) {
        cJSON_Delete(r);
        cJSON_Delete(source_ids);
        cJSON_Delete(backends);
        send_error(state, fd, "failed to allocate audio capabilities response");
        return;
    }

    add_audio_source_id_if_registered(source_ids, "pulse_output_capture");
    add_audio_source_id_if_registered(source_ids, "pulse_input_capture");
    add_audio_source_id_if_registered(source_ids, "pipewire_audio_output_capture");
    add_audio_source_id_if_registered(source_ids, "pipewire_audio_input_capture");
    add_audio_source_id_if_registered(source_ids, "pipewire_audio_application_capture");

    if (obs_source_get_display_name("pulse_output_capture") ||
        obs_source_get_display_name("pulse_input_capture"))
        add_unique_string(backends, "pulse");
    if (obs_source_get_display_name("pipewire_audio_output_capture") ||
        obs_source_get_display_name("pipewire_audio_input_capture") ||
        obs_source_get_display_name("pipewire_audio_application_capture"))
        add_unique_string(backends, "pipewire");

    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddNumberToObject(r, "max_track_count", CLIPPER_MAX_AUDIO_TRACKS);
    cJSON_AddBoolToObject(
        r,
        "app_capture_available",
        obs_source_get_display_name("pipewire_audio_application_capture") != NULL);
    cJSON_AddItemToObject(r, "source_ids", source_ids);
    cJSON_AddItemToObject(r, "supported_backends", backends);

    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void add_runtime_audio_source(cJSON *array, const char *id, const char *kind,
                                     const char *display_name, const char *backend)
{
    cJSON *source = cJSON_CreateObject();
    if (!source)
        return;

    cJSON_AddStringToObject(source, "id", id);
    cJSON_AddStringToObject(source, "kind", kind);
    cJSON_AddStringToObject(source, "display_name", display_name);
    cJSON_AddStringToObject(source, "backend", backend);
    cJSON_AddItemToArray(array, source);
}

static const char *json_object_string(cJSON *object, const char *key)
{
    cJSON *item = cJSON_GetObjectItem(object, key);
    return item && cJSON_IsString(item) ? item->valuestring : NULL;
}

static int json_object_int(cJSON *object, const char *key)
{
    cJSON *item = cJSON_GetObjectItem(object, key);
    if (!item)
        return 0;
    if (cJSON_IsNumber(item))
        return item->valueint;
    if (cJSON_IsString(item) && item->valuestring && *item->valuestring)
        return atoi(item->valuestring);
    return 0;
}

static char *read_command_output(const char *command, size_t max_bytes)
{
    FILE *pipe = popen(command, "r");
    if (!pipe)
        return NULL;

    char *buffer = malloc(max_bytes + 1);
    if (!buffer) {
        pclose(pipe);
        return NULL;
    }

    size_t used = 0;
    while (used < max_bytes) {
        size_t nread = fread(buffer + used, 1, max_bytes - used, pipe);
        used += nread;
        if (nread == 0)
            break;
    }
    buffer[used] = '\0';
    pclose(pipe);

    if (used == 0) {
        free(buffer);
        return NULL;
    }

    return buffer;
}

static const char *first_nonempty_string(const char *a, const char *b, const char *c)
{
    if (a && *a)
        return a;
    if (b && *b)
        return b;
    if (c && *c)
        return c;
    return "";
}

static bool is_generic_audio_client_name(const char *name)
{
    return !name || !*name ||
           strcmp(name, "WEBRTC VoiceEngine") == 0 ||
           strcmp(name, "Chromium input") == 0 ||
           strcmp(name, "Chromium output") == 0 ||
           strcmp(name, "AudioIPC Server") == 0 ||
           strcmp(name, "ALSA Playback") == 0;
}

static const char *flatpak_app_name(const char *app_id)
{
    if (!app_id || !*app_id)
        return "";

    const char *last_dot = strrchr(app_id, '.');
    return last_dot && last_dot[1] ? last_dot + 1 : app_id;
}

static const char *friendly_audio_display_name(const char *app_name,
                                               const char *binary,
                                               const char *node_name,
                                               const char *app_id)
{
    if (!is_generic_audio_client_name(app_name))
        return app_name;
    if (binary && *binary)
        return binary;

    const char *flatpak_name = flatpak_app_name(app_id);
    if (flatpak_name && *flatpak_name)
        return flatpak_name;

    return first_nonempty_string(app_name, node_name, "");
}

static bool is_hidden_audio_client(const char *name)
{
    if (!name || !*name)
        return false;

    const char *hidden[] = {
        "clipper-engine",
        "kded6",
        "kwin_wayland",
        "libcanberra",
        "pactl",
        "pipewire",
        "pipewire-pulse",
        "plasmashell",
        "uresourced",
        "wireplumber",
        "WirePlumber",
        "WirePlumber [export]",
        "xdg-desktop-portal",
        NULL,
    };

    for (size_t i = 0; hidden[i]; i++) {
        if (strcmp(name, hidden[i]) == 0)
            return true;
    }

    return false;
}

static bool should_show_pactl_client_source(const char *display_name,
                                            const char *app_name,
                                            const char *binary)
{
    return !is_hidden_audio_client(display_name) &&
           !is_hidden_audio_client(app_name) &&
           !is_hidden_audio_client(binary);
}

static bool audio_source_has_app_identity(cJSON *source, const char *binary,
                                          const char *app_name,
                                          const char *display_name)
{
    const char *kind = json_object_string(source, "kind");
    if (!kind || strcmp(kind, "application") != 0)
        return false;

    const char *existing_binary = json_object_string(source, "binary");
    const char *existing_app_name = json_object_string(source, "app_name");
    const char *existing_display_name = json_object_string(source, "display_name");

    if (binary && *binary && existing_binary && strcmp(existing_binary, binary) == 0)
        return true;
    if (app_name && *app_name && existing_app_name &&
        strcmp(existing_app_name, app_name) == 0)
        return true;
    if (display_name && *display_name && existing_display_name &&
        strcmp(existing_display_name, display_name) == 0)
        return true;

    return false;
}

static bool has_application_audio_source(cJSON *sources, const char *binary,
                                         const char *app_name,
                                         const char *display_name)
{
    cJSON *source = NULL;
    cJSON_ArrayForEach(source, sources) {
        if (cJSON_IsObject(source) &&
            audio_source_has_app_identity(source, binary, app_name, display_name))
            return true;
    }

    return false;
}

static void add_application_audio_source(cJSON *sources, const char *id,
                                         const char *display_name,
                                         const char *app_name,
                                         const char *binary, int pid,
                                         int client_id, int node_id,
                                         const char *media_name,
                                         const char *backend)
{
    if (!display_name || !*display_name || !id || !*id)
        return;

    if (has_application_audio_source(sources, binary, app_name, display_name))
        return;

    cJSON *source = cJSON_CreateObject();
    if (!source)
        return;

    cJSON_AddStringToObject(source, "id", id);
    cJSON_AddStringToObject(source, "kind", "application");
    cJSON_AddStringToObject(source, "display_name", display_name);
    cJSON_AddStringToObject(source, "app_name", app_name && *app_name ? app_name : "");
    cJSON_AddStringToObject(source, "binary", binary && *binary ? binary : "");
    cJSON_AddNumberToObject(source, "pid", pid);
    cJSON_AddNumberToObject(source, "client_id", client_id);
    cJSON_AddNumberToObject(source, "node_id", node_id);
    cJSON_AddStringToObject(
        source, "media_name", media_name && *media_name ? media_name : "");
    cJSON_AddStringToObject(source, "backend", backend && *backend ? backend : "pulse");
    cJSON_AddItemToArray(sources, source);
}

static bool string_ends_with(const char *value, const char *suffix)
{
    if (!value || !suffix)
        return false;

    size_t value_len = strlen(value);
    size_t suffix_len = strlen(suffix);
    return value_len >= suffix_len &&
           strcmp(value + value_len - suffix_len, suffix) == 0;
}

static void add_device_audio_source(cJSON *sources, const char *id,
                                    const char *kind,
                                    const char *device_id,
                                    const char *display_name,
                                    const char *backend)
{
    if (!device_id || !*device_id || !display_name || !*display_name)
        return;

    cJSON *source = cJSON_CreateObject();
    if (!source)
        return;

    cJSON_AddStringToObject(source, "id", id && *id ? id : device_id);
    cJSON_AddStringToObject(source, "kind", kind);
    cJSON_AddStringToObject(source, "backend", backend && *backend ? backend : "pulse");
    cJSON_AddStringToObject(source, "device_id", device_id);
    cJSON_AddStringToObject(source, "display_name", display_name);
    cJSON_AddItemToArray(sources, source);
}

static void add_pactl_recording_source(cJSON *sources, cJSON *source_item)
{
    const char *name = json_object_string(source_item, "name");
    const char *description = json_object_string(source_item, "description");
    const char *driver = json_object_string(source_item, "driver");
    cJSON *props = cJSON_GetObjectItem(source_item, "properties");
    const char *device_class =
        props && cJSON_IsObject(props) ? json_object_string(props, "device.class") : NULL;

    if (!name || !*name || string_ends_with(name, ".monitor"))
        return;
    if (device_class && strcmp(device_class, "monitor") == 0)
        return;

    char id[64];
    int index = json_object_int(source_item, "index");
    if (index > 0)
        snprintf(id, sizeof(id), "source_%d", index);
    else
        copy_string(id, sizeof(id), name);

    add_device_audio_source(
        sources,
        id,
        "input_device",
        name,
        description && *description ? description : name,
        driver && string_contains_ci(driver, "pipewire") ? "pipewire" : "pulse");
}

static void add_pactl_recording_sources(cJSON *sources)
{
    char *json_text = read_command_output(
        "pactl -f json list sources 2>/dev/null", 1024 * 1024);
    if (!json_text)
        return;

    cJSON *root = cJSON_Parse(json_text);
    free(json_text);
    if (!root)
        return;

    if (!cJSON_IsArray(root)) {
        cJSON_Delete(root);
        return;
    }

    cJSON *source_item = NULL;
    cJSON_ArrayForEach(source_item, root) {
        if (cJSON_IsObject(source_item))
            add_pactl_recording_source(sources, source_item);
    }

    cJSON_Delete(root);
}

static void add_pactl_sink_input_source(cJSON *sources, cJSON *sink_input)
{
    cJSON *props = cJSON_GetObjectItem(sink_input, "properties");
    if (!props || !cJSON_IsObject(props))
        return;

    int index = json_object_int(sink_input, "index");
    const char *app_name = json_object_string(props, "application.name");
    const char *binary = json_object_string(props, "application.process.binary");
    const char *media_name = json_object_string(props, "media.name");
    const char *node_name = json_object_string(props, "node.name");
    const char *app_id = json_object_string(props, "pipewire.access.portal.app_id");
    const char *driver = json_object_string(sink_input, "driver");
    const char *display_name =
        friendly_audio_display_name(app_name, binary, node_name, app_id);

    if (!*display_name || index <= 0)
        return;

    char id[64];
    snprintf(id, sizeof(id), "sink_input_%d", index);

    add_application_audio_source(
        sources,
        id,
        display_name,
        app_name,
        binary,
        json_object_int(props, "application.process.id"),
        json_object_int(props, "client.id"),
        json_object_int(props, "object.id"),
        media_name,
        driver && string_contains_ci(driver, "pipewire") ? "pipewire" : "pulse");
}

static void add_pactl_sink_inputs(cJSON *sources)
{
    char *json_text = read_command_output(
        "pactl -f json list sink-inputs 2>/dev/null", 1024 * 1024);
    if (!json_text)
        return;

    cJSON *root = cJSON_Parse(json_text);
    free(json_text);
    if (!root)
        return;

    if (!cJSON_IsArray(root)) {
        cJSON_Delete(root);
        return;
    }

    cJSON *sink_input = NULL;
    cJSON_ArrayForEach(sink_input, root) {
        if (cJSON_IsObject(sink_input))
            add_pactl_sink_input_source(sources, sink_input);
    }

    cJSON_Delete(root);
}

static void add_pactl_client_source(cJSON *sources, cJSON *client)
{
    cJSON *props = cJSON_GetObjectItem(client, "properties");
    if (!props || !cJSON_IsObject(props))
        return;

    int index = json_object_int(client, "index");
    const char *app_name = json_object_string(props, "application.name");
    const char *binary = json_object_string(props, "application.process.binary");
    const char *node_name = json_object_string(props, "node.name");
    const char *app_id = json_object_string(props, "pipewire.access.portal.app_id");
    const char *driver = json_object_string(client, "driver");
    const char *display_name =
        friendly_audio_display_name(app_name, binary, node_name, app_id);

    if (!*display_name || index <= 0)
        return;
    if (!should_show_pactl_client_source(display_name, app_name, binary))
        return;

    char id[64];
    snprintf(id, sizeof(id), "client_%d", index);

    add_application_audio_source(
        sources,
        id,
        display_name,
        app_name,
        binary,
        json_object_int(props, "application.process.id"),
        index,
        0,
        "",
        driver && string_contains_ci(driver, "pipewire") ? "pipewire" : "pulse");
}

static void add_pactl_clients(cJSON *sources)
{
    char *json_text = read_command_output(
        "pactl -f json list clients 2>/dev/null", 1024 * 1024);
    if (!json_text)
        return;

    cJSON *root = cJSON_Parse(json_text);
    free(json_text);
    if (!root)
        return;

    if (!cJSON_IsArray(root)) {
        cJSON_Delete(root);
        return;
    }

    cJSON *client = NULL;
    cJSON_ArrayForEach(client, root) {
        if (cJSON_IsObject(client))
            add_pactl_client_source(sources, client);
    }

    cJSON_Delete(root);
}

static void handle_list_audio_sources(engine_state_t *state, int fd)
{
    (void)state;

    cJSON *r = cJSON_CreateObject();
    cJSON *sources = cJSON_CreateArray();
    if (!r || !sources) {
        cJSON_Delete(r);
        cJSON_Delete(sources);
        send_error(state, fd, "failed to allocate audio sources response");
        return;
    }

    if (obs_source_get_display_name("pulse_output_capture")) {
        add_runtime_audio_source(
            sources, "pulse_output_default", "output_device", "System Audio", "pulse");
    }
    if (obs_source_get_display_name("pulse_input_capture")) {
        add_device_audio_source(
            sources,
            "pulse_input_default",
            "input_device",
            "default",
            "System default",
            "pulse");
    }
    add_pactl_recording_sources(sources);
    add_pactl_sink_inputs(sources);
    add_pactl_clients(sources);

    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddItemToObject(r, "sources", sources);
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void parse_audio_volume_update(engine_state_t *state, cJSON *audio)
{
    if (!audio || !cJSON_IsObject(audio))
        return;

    cJSON *mic = cJSON_GetObjectItem(audio, "microphone");
    if (mic && cJSON_IsObject(mic)) {
        cJSON *volume = cJSON_GetObjectItem(mic, "volume");
        if (volume && cJSON_IsNumber(volume))
            state->mic_volume = clamp_audio_volume(volume->valuedouble);
    }

    cJSON *tracks = cJSON_GetObjectItem(audio, "tracks");
    if (!tracks || !cJSON_IsArray(tracks))
        return;

    cJSON *track_item = NULL;
    cJSON_ArrayForEach(track_item, tracks) {
        if (!cJSON_IsObject(track_item))
            continue;

        cJSON *track_number_item = cJSON_GetObjectItem(track_item, "track");
        if (!track_number_item || !cJSON_IsNumber(track_number_item))
            continue;

        int track_number = track_number_item->valueint;
        if (track_number < 1 || track_number > CLIPPER_MAX_AUDIO_TRACKS)
            continue;

        cJSON *volume = cJSON_GetObjectItem(track_item, "volume");
        if (volume && cJSON_IsNumber(volume))
            state->audio_tracks[track_number - 1].volume =
                clamp_audio_volume(volume->valuedouble);
    }
}

static float current_volume_for_audio_source(engine_state_t *state, size_t idx)
{
    if (idx >= state->audio_source_count)
        return 1.0f;

    if (state->audio_mode == AUDIO_MODE_SPLIT_TRACKS) {
        int track = state->audio_source_tracks[idx];
        if (track >= 1 && track <= CLIPPER_MAX_AUDIO_TRACKS)
            return state->audio_tracks[track - 1].volume;
        return 1.0f;
    }

    if (state->audio_source_kinds[idx] == AUDIO_SOURCE_INPUT_DEVICE)
        return state->mic_volume;

    return 1.0f;
}

static int apply_audio_source_volumes(engine_state_t *state)
{
    int applied = 0;
    for (size_t i = 0; i < state->audio_source_count; i++) {
        if (!state->audio_sources[i])
            continue;

        obs_source_set_volume(
            state->audio_sources[i], current_volume_for_audio_source(state, i));
        applied++;
    }
    return applied;
}

static void handle_update_audio_volumes(engine_state_t *state, int fd, cJSON *json)
{
    cJSON *audio = cJSON_GetObjectItem(json, "audio");
    if (!audio || !cJSON_IsObject(audio)) {
        send_error(state, fd, "missing or invalid 'audio' field");
        return;
    }

    parse_audio_volume_update(state, audio);
    int applied = apply_audio_source_volumes(state);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    cJSON_AddNumberToObject(r, "applied_sources", applied);
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void handle_start_replay(engine_state_t *state, int fd)
{
    if (state->buffer_active) {
        send_error(state, fd, "already running");
        return;
    }

    if (!start_replay_buffer_internal(state, true)) {
        const char *err = state->replay_output ?
            obs_output_get_last_error(state->replay_output) : NULL;
        send_error(state, fd, err ? err : "failed to start replay buffer");
        return;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void handle_stop_replay(engine_state_t *state, int fd)
{
    if (!state->buffer_active) {
        send_error(state, fd, "not running");
        return;
    }

    obs_output_stop(state->replay_output);
    state->buffer_active = false;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    send_to_client(state, fd, r);
    cJSON_Delete(r);

    broadcast_status_changed(state, "idle");
}

static void handle_save_replay(engine_state_t *state, int fd, cJSON *json)
{
    if (!state->buffer_active) {
        send_error(state, fd, "not running");
        return;
    }

    const char *format = "%CCYY-%MM-%DD_%hh-%mm-%ss";
    char game_name[CLIP_FILENAME_GAME_MAX];
    char format_with_game[CLIP_FILENAME_GAME_MAX + 32];
    const cJSON *game_name_item = cJSON_GetObjectItem(json, "game_name");
    if (game_name_item && cJSON_IsString(game_name_item)) {
        sanitize_clip_filename_part(
            game_name_item->valuestring, game_name, sizeof(game_name));
        if (game_name[0] != '\0') {
            snprintf(
                format_with_game, sizeof(format_with_game),
                "%s_%%CCYY-%%MM-%%DD_%%hh-%%mm-%%ss", game_name);
            format = format_with_game;
        }
    }

    obs_data_t *settings = obs_output_get_settings(state->replay_output);
    obs_data_set_string(settings, "format", format);
    obs_output_update(state->replay_output, settings);
    obs_data_release(settings);

    calldata_t cd;
    memset(&cd, 0, sizeof(cd));
    proc_handler_t *ph = obs_output_get_proc_handler(state->replay_output);
    bool ok = proc_handler_call(ph, "save", &cd);
    calldata_free(&cd);

    if (!ok) {
        send_error(state, fd, "save proc not found");
        return;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    send_to_client(state, fd, r);
    cJSON_Delete(r);
}

static void handle_shutdown_cmd(engine_state_t *state, int fd, cJSON *json)
{
    /* Check if restart was requested */
    const cJSON *restart_item = cJSON_GetObjectItem(json, "restart");
    if (restart_item && cJSON_IsBool(restart_item) && cJSON_IsTrue(restart_item)) {
        g_restart_requested = 1;
    }

    /* Send ack to the requesting client before setting the shutdown flag */
    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    send_to_client(state, fd, r);
    cJSON_Delete(r);

    g_shutdown = 1;
}

static void dispatch_command(engine_state_t *state, int fd, const char *line)
{
    cJSON *json = cJSON_Parse(line);
    if (!json) {
        send_error(state, fd, "invalid JSON");
        return;
    }

    const cJSON *cmd_item = cJSON_GetObjectItem(json, "cmd");
    if (!cmd_item || !cJSON_IsString(cmd_item)) {
        send_error(state, fd, "missing or invalid 'cmd' field");
        cJSON_Delete(json);
        return;
    }

    const char *cmd = cmd_item->valuestring;

    if      (strcmp(cmd, "get_status")          == 0) handle_get_status(state, fd);
    else if (strcmp(cmd, "get_capabilities")    == 0) handle_get_capabilities(state, fd);
    else if (strcmp(cmd, "get_audio_capabilities") == 0) handle_get_audio_capabilities(state, fd);
    else if (strcmp(cmd, "list_audio_sources")  == 0) handle_list_audio_sources(state, fd);
    else if (strcmp(cmd, "start_replay_buffer")  == 0) handle_start_replay(state, fd);
    else if (strcmp(cmd, "stop_replay_buffer")   == 0) handle_stop_replay(state, fd);
    else if (strcmp(cmd, "save_replay_buffer")   == 0) handle_save_replay(state, fd, json);
    else if (strcmp(cmd, "get_preview_frame")    == 0) handle_get_preview_frame(state, fd, json);
    else if (strcmp(cmd, "update_audio_volumes") == 0) handle_update_audio_volumes(state, fd, json);
    else if (strcmp(cmd, "shutdown")             == 0) handle_shutdown_cmd(state, fd, json);
    else    send_error(state, fd, "unknown command");

    cJSON_Delete(json);
}

static void pump_glib_main_context(void)
{
    while (g_main_context_iteration(NULL, FALSE)) {
        /* Drain pending portal/PipeWire async callbacks without blocking. */
    }
}

static bool get_config_path(char *buf, size_t size)
{
    const char *config_home = getenv("XDG_CONFIG_HOME");
    const char *home = getenv("HOME");
    if (!buf || size == 0)
        return false;

    if (config_home && *config_home) {
        snprintf(buf, size, "%s/clipper/config.json", config_home);
        return true;
    }
    if (!home || !*home)
        return false;

    snprintf(buf, size, "%s/.config/clipper/config.json", home);
    return true;
}

static bool load_config_json_for_update(const char *config_path, cJSON **root_out)
{
    *root_out = NULL;

    FILE *f = fopen(config_path, "r");
    if (!f) {
        if (errno == ENOENT) {
            *root_out = cJSON_CreateObject();
            return *root_out != NULL;
        }
        fprintf(stderr, "[engine] WARN: could not open config for update '%s': %s\n",
                config_path, strerror(errno));
        return false;
    }

    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);

    if (fsize <= 0 || fsize > 1024 * 1024) {
        fclose(f);
        *root_out = cJSON_CreateObject();
        return *root_out != NULL;
    }

    char *json_str = malloc((size_t)fsize + 1);
    if (!json_str) {
        fclose(f);
        return false;
    }

    size_t nread = fread(json_str, 1, (size_t)fsize, f);
    json_str[nread] = '\0';
    fclose(f);

    cJSON *root = cJSON_Parse(json_str);
    free(json_str);

    if (!root || !cJSON_IsObject(root)) {
        cJSON_Delete(root);
        fprintf(stderr, "[engine] WARN: refusing to update invalid config JSON\n");
        return false;
    }

    *root_out = root;
    return true;
}

static bool write_config_json_atomic(const char *config_path, cJSON *root)
{
    char dir[PATH_MAX];
    copy_string(dir, sizeof(dir), config_path);
    char *slash = strrchr(dir, '/');
    if (!slash)
        return false;
    *slash = '\0';

    if (mkdirs(dir) < 0) {
        fprintf(stderr, "[engine] WARN: could not create config dir '%s': %s\n",
                dir, strerror(errno));
        return false;
    }

    char tmp_path[PATH_MAX];
    const char *tmp_suffix = "/.config.json.tmp.XXXXXX";
    size_t dir_len = strlen(dir);
    size_t suffix_len = strlen(tmp_suffix);
    if (dir_len + suffix_len >= sizeof(tmp_path)) {
        fprintf(stderr, "[engine] WARN: config temp path too long\n");
        return false;
    }
    memcpy(tmp_path, dir, dir_len);
    memcpy(tmp_path + dir_len, tmp_suffix, suffix_len + 1);

    int fd = mkstemp(tmp_path);
    if (fd < 0) {
        fprintf(stderr, "[engine] WARN: could not create config temp file: %s\n",
                strerror(errno));
        return false;
    }

    FILE *tmp = fdopen(fd, "w");
    if (!tmp) {
        close(fd);
        unlink(tmp_path);
        return false;
    }

    char *printed = cJSON_Print(root);
    if (!printed) {
        fclose(tmp);
        unlink(tmp_path);
        return false;
    }

    bool ok = fputs(printed, tmp) >= 0 && fputc('\n', tmp) != EOF;
    free(printed);

    if (fclose(tmp) != 0)
        ok = false;

    if (!ok) {
        unlink(tmp_path);
        return false;
    }

    if (rename(tmp_path, config_path) != 0) {
        fprintf(stderr, "[engine] WARN: could not replace config file: %s\n",
                strerror(errno));
        unlink(tmp_path);
        return false;
    }

    return true;
}

static bool persist_config_string(const char *key, const char *value)
{
    char config_path[PATH_MAX];
    if (!get_config_path(config_path, sizeof(config_path)))
        return false;

    cJSON *root = NULL;
    if (!load_config_json_for_update(config_path, &root))
        return false;

    cJSON_DeleteItemFromObjectCaseSensitive(root, key);
    cJSON_AddStringToObject(root, key, value ? value : "");

    bool ok = write_config_json_atomic(config_path, root);
    cJSON_Delete(root);
    return ok;
}

static void maybe_persist_pipewire_restore_token(engine_state_t *state,
                                                  const struct timespec *now)
{
    if (state->capture_mode != CAPTURE_MODE_DISPLAY || !state->display_source ||
        strcmp(obs_source_get_id(state->display_source),
               "pipewire-screen-capture-source") != 0)
        return;

    long elapsed_ms =
        (now->tv_sec - state->last_restore_token_check.tv_sec) * 1000 +
        (now->tv_nsec - state->last_restore_token_check.tv_nsec) / 1000000;
    if (elapsed_ms < 1000)
        return;
    state->last_restore_token_check = *now;

    obs_source_save(state->display_source);
    obs_data_t *settings = obs_source_get_settings(state->display_source);
    if (!settings)
        return;

    const char *token = obs_data_get_string(settings, "RestoreToken");
    if (token && *token && strcmp(token, state->pipewire_restore_token) != 0) {
        copy_string(state->pipewire_restore_token,
                    sizeof(state->pipewire_restore_token), token);

        if (persist_config_string("pipewire_restore_token", token)) {
            fprintf(stderr, "[engine] saved PipeWire restore token\n");
        } else {
            fprintf(stderr, "[engine] WARN: failed to save PipeWire restore token\n");
        }
    }

    obs_data_release(settings);
}

/* ── Process incoming data from a connected client ───────────────────────── */
static void process_client_data(engine_state_t *state, int idx)
{
    client_t *c = &state->clients[idx];

    /* How many bytes fit in the buffer (leave room for NUL) */
    int space = CLIENT_BUF_SIZE - c->buf_len - 1;
    if (space <= 0) {
        /* Buffer overflowed — disconnect the client */
        fprintf(stderr, "[engine] client %d buffer overflow, disconnecting\n", idx);
        close_client(state, idx);
        return;
    }

    ssize_t n = read(c->fd, c->buf + c->buf_len, (size_t)space);
    if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        close_client(state, idx);
        return;
    }
    if (n == 0) { /* EOF — client disconnected */
        if (g_verbose)
            fprintf(stderr, "[engine] client %d disconnected\n", idx);
        close_client(state, idx);
        return;
    }

    /* Save fd before any potential close_client call during dispatch */
    int client_fd = c->fd;
    c->buf_len += (int)n;

    /* Scan for complete newline-terminated lines */
    int start = 0;
    for (int i = 0; i < c->buf_len; i++) {
        if (c->buf[i] == '\n') {
            c->buf[i] = '\0';
            /* Strip trailing CR (for telnet-style clients) */
            if (i > start && c->buf[i - 1] == '\r')
                c->buf[i - 1] = '\0';
            /* Dispatch if non-empty */
            if (c->buf[start])
                dispatch_command(state, client_fd, c->buf + start);
            start = i + 1;
        }
    }

    /* Shift any incomplete line to the front of the buffer */
    int remaining = c->buf_len - start;
    if (remaining > 0 && start > 0)
        memmove(c->buf, c->buf + start, (size_t)remaining);
    c->buf_len = remaining;
}

/* ── Main event loop ─────────────────────────────────────────────────────── */
static void event_loop(engine_state_t *state)
{
    struct pollfd fds[1 + MAX_CLIENTS];

    while (!g_shutdown) {
        pump_glib_main_context();

        /*
         * Snapshot active client indices under the mutex.
         * client.fd is only written by the main thread (accept/close), so
         * reading it here (on the main thread) without the mutex is safe.
         * The mutex is required only for writes TO the fd.
         */
        int client_indices[MAX_CLIENTS];
        int num_clients = 0;

        pthread_mutex_lock(&state->clients_mutex);
        for (int i = 0; i < MAX_CLIENTS; i++) {
            if (state->clients[i].active)
                client_indices[num_clients++] = i;
        }
        pthread_mutex_unlock(&state->clients_mutex);

        /* Build poll array: [server_fd, client_fd_0, client_fd_1, ...] */
        int nfds = 0;
        fds[nfds].fd      = state->server_fd;
        fds[nfds].events  = POLLIN;
        fds[nfds].revents = 0;
        nfds++;

        for (int i = 0; i < num_clients; i++) {
            fds[nfds].fd      = state->clients[client_indices[i]].fd;
            fds[nfds].events  = POLLIN;
            fds[nfds].revents = 0;
            nfds++;
        }

        int ret = poll(fds, (nfds_t)nfds, 500 /* ms */);
        pump_glib_main_context();

        if (ret < 0) {
            if (errno == EINTR) continue;
            perror("[engine] poll");
            break;
        }

        /* Check for hook state changes every 500ms */
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        maybe_persist_pipewire_restore_token(state, &now);

        long elapsed_ms = (now.tv_sec - state->last_hook_check.tv_sec) * 1000 +
                          (now.tv_nsec - state->last_hook_check.tv_nsec) / 1000000;
        
        if (elapsed_ms >= 500 &&
            state->capture_mode == CAPTURE_MODE_GAME &&
            state->vkc_source) {
            state->last_hook_check = now;
            
            proc_handler_t *src_ph = obs_source_get_proc_handler(state->vkc_source);
            if (src_ph) {
                calldata_t cd = {0};
                bool call_ok = proc_handler_call(src_ph, "get_hooked", &cd);
                
                bool hooked = false;
                const char *exe = NULL;
                
                if (call_ok) {
                    hooked = calldata_bool(&cd, "hooked");
                    exe = calldata_string(&cd, "executable");
                    if (g_verbose && !state->logged_hook_poll) {
                        fprintf(stderr,
                                "[engine] vkcapture get_hooked available: hooked=%d exe=%s\n",
                                hooked ? 1 : 0, exe ? exe : "(none)");
                        state->logged_hook_poll = true;
                    }
                } else {
                    /*
                     * Upstream obs-vkcapture does not expose a get_hooked
                     * source proc.  Its public source dimensions become
                     * non-zero as soon as the first captured DMA-BUF frame
                     * arrives, and return to zero when the client disconnects.
                     * Use that state so a working hook actually starts and
                     * stops Clipper's replay buffer.
                     */
                    uint32_t width = obs_source_get_width(state->vkc_source);
                    uint32_t height = obs_source_get_height(state->vkc_source);
                    hooked = width > 0 && height > 0;
                    if (!state->logged_missing_hook_proc) {
                        fprintf(stderr,
                                "[engine] vkcapture hook detection using source frames\n");
                        state->logged_missing_hook_proc = true;
                    }
                    if (g_verbose && hooked && !state->logged_hook_poll) {
                        fprintf(stderr,
                                "[engine] vkcapture frames available: %ux%u\n",
                                width, height);
                        state->logged_hook_poll = true;
                    }
                }
                
                /* Detect state transitions */
                if (hooked && !state->game_hooked) {
                    /* Transition: unhooked → hooked */
                    if (exe) {
                        strncpy(state->game_exe, exe, sizeof(state->game_exe) - 1);
                        state->game_exe[sizeof(state->game_exe) - 1] = '\0';
                    }
                    fprintf(stderr, "[engine] Game hooked: %s\n", exe ? exe : "(unknown)");
                    
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "event", "game_connected");
                    cJSON_AddStringToObject(ev, "executable", 
                                          state->game_exe[0] ? state->game_exe : "unknown");
                    broadcast_event(state, ev);
                    cJSON_Delete(ev);
                    
                    /* Auto-start replay buffer */
                    if (!state->buffer_active && state->replay_output) {
                        if (start_replay_buffer_internal(state, true))
                            fprintf(stderr, "[engine] Auto-started replay buffer\n");
                    }
                    
                    state->game_hooked = true;
                } else if (!hooked && state->game_hooked) {
                    /* Transition: hooked → unhooked */
                    fprintf(stderr, "[engine] Game unhooked: %s\n", state->game_exe);
                    
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "event", "game_disconnected");
                    broadcast_event(state, ev);
                    cJSON_Delete(ev);
                    
                    /* Auto-stop replay buffer */
                    if (state->buffer_active && state->replay_output) {
                        obs_output_stop(state->replay_output);
                        state->buffer_active = false;
                        broadcast_status_changed(state, "idle");
                        fprintf(stderr, "[engine] Auto-stopped replay buffer\n");
                    }
                    
                    state->game_hooked = false;
                    state->game_exe[0] = '\0';
                }
                
                calldata_free(&cd);
            } else if (g_verbose && !state->logged_missing_hook_proc) {
                fprintf(stderr, "[engine] WARN: vkcapture source has no proc handler yet\n");
                state->logged_missing_hook_proc = true;
            }
        }
        
        if (ret == 0) continue; /* timeout — re-check g_shutdown */

        /* New incoming connection */
        if (fds[0].revents & POLLIN)
            accept_client(state);

        /* Readable/error events on client fds */
        for (int i = 1; i < nfds; i++) {
            if (fds[i].revents & (POLLIN | POLLHUP | POLLERR))
                process_client_data(state, client_indices[i - 1]);
        }
    }
}

/* ── Encoder settings ─────────────────────────────────────────────────────── */

/**
 * Create video encoder settings based on encoder type and rate control.
 * Returns obs_data_t* that must be released by caller.
 */
static obs_data_t *create_video_encoder_settings(const char *encoder_id,
                                                  video_rate_control_t rate_control,
                                                  int quality_cqp,
                                                  int bitrate,
                                                  int max_bitrate,
                                                  const char *vaapi_device)
{
    obs_data_t *settings = obs_data_create();
    bool is_x264 = strcmp(encoder_id, "obs_x264") == 0;
    bool is_vaapi = strcmp(encoder_id, "ffmpeg_vaapi") == 0 ||
                    strcmp(encoder_id, "ffmpeg_vaapi_tex") == 0 ||
                    strcmp(encoder_id, "h264_vaapi") == 0 ||
                    strcmp(encoder_id, "h264_vaapi_tex") == 0 ||
                    strcmp(encoder_id, "hevc_ffmpeg_vaapi") == 0 ||
                    strcmp(encoder_id, "av1_ffmpeg_vaapi") == 0;

    if (max_bitrate < bitrate)
        max_bitrate = bitrate;

    if (strcmp(encoder_id, "obs_x264") == 0) {
        obs_data_set_string(settings, "profile", "high");
        /* OBS's replay workload is real-time.  The old medium preset creates
         * large look-ahead/thread pools and caused excessive memory use at
         * the default 1080p60 settings. */
        obs_data_set_string(settings, "preset", "veryfast");

        if (rate_control == VIDEO_RATE_CONTROL_CQP) {
            obs_data_set_string(settings, "rate_control", "CRF");
            obs_data_set_int(settings, "crf", quality_cqp);
        } else if (rate_control == VIDEO_RATE_CONTROL_CBR) {
            obs_data_set_string(settings, "rate_control", "CBR");
            obs_data_set_int(settings, "bitrate", bitrate);
            obs_data_set_bool(settings, "use_bufsize", false);
            obs_data_set_int(settings, "buffer_size", bitrate);
        } else {
            obs_data_set_string(settings, "rate_control", "VBR");
            obs_data_set_int(settings, "bitrate", bitrate);
            obs_data_set_bool(settings, "use_bufsize", true);
            obs_data_set_int(settings, "buffer_size", max_bitrate);
            obs_data_set_int(settings, "crf", quality_cqp);
        }

    } else if (strcmp(encoder_id, "ffmpeg_vaapi") == 0) {
        /* VAAPI hardware encoder */
        if (vaapi_device && strcmp(vaapi_device, "auto") != 0)
            obs_data_set_string(settings, "vaapi_device", vaapi_device);
        obs_data_set_string(settings, "profile", "high");
    } else if (strcmp(encoder_id, "ffmpeg_svt_av1") == 0) {
        /* Preset 10 is intended for real-time use and avoids SVT-AV1's much
         * larger default worker/look-ahead allocation. */
        obs_data_set_int(settings, "preset", 10);
    }

    if (!is_x264) {
        const char *obs_rate_control = "CQP";
        if (rate_control == VIDEO_RATE_CONTROL_CBR)
            obs_rate_control = "CBR";
        else if (rate_control == VIDEO_RATE_CONTROL_VBR)
            obs_rate_control = "VBR";

        obs_data_set_string(settings, "rate_control", obs_rate_control);
    }

    if (!is_x264 && rate_control == VIDEO_RATE_CONTROL_CQP) {
        obs_data_set_int(settings, "qp", quality_cqp);
        obs_data_set_int(settings, "cqp", quality_cqp);
        obs_data_set_int(settings, "crf", quality_cqp);
    } else if (!is_x264 && rate_control == VIDEO_RATE_CONTROL_CBR) {
        obs_data_set_int(settings, "bitrate", bitrate);
        obs_data_set_int(settings, "max_bitrate", bitrate);
        obs_data_set_int(settings, "maxrate", bitrate);
    } else if (!is_x264 && rate_control == VIDEO_RATE_CONTROL_VBR) {
        obs_data_set_int(settings, "bitrate", bitrate);
        obs_data_set_int(settings, "max_bitrate", max_bitrate);
        obs_data_set_int(settings, "maxrate", max_bitrate);
    }

    if (is_vaapi && vaapi_device && strcmp(vaapi_device, "auto") != 0)
        obs_data_set_string(settings, "vaapi_device", vaapi_device);
    
    return settings;
}

/**
 * Create audio encoder settings.
 * Returns obs_data_t* that must be released by caller.
 */
static obs_data_t *create_audio_encoder_settings(const char *encoder_id, int bitrate)
{
    obs_data_t *settings = obs_data_create();
    
    if (strcmp(encoder_id, "ffmpeg_aac") == 0) {
        /* AAC audio encoder: set bitrate in kbps */
        obs_data_set_int(settings, "bitrate", bitrate);
    }
    /* Other encoders: return empty settings */
    
    return settings;
}

static obs_data_t *create_pulse_device_settings(const char *device_id)
{
    obs_data_t *settings = obs_data_create();
    if (settings && device_id && *device_id && strcmp(device_id, "default") != 0)
        obs_data_set_string(settings, "device_id", device_id);
    return settings;
}

static obs_source_t *create_audio_source_instance(const audio_source_config_t *config,
                                                  size_t ordinal)
{
    if (!config)
        return NULL;

    char name[128];
    snprintf(name, sizeof(name), "clipper_audio_%zu_%s", ordinal,
             audio_source_kind_to_string(config->kind));

    if (config->kind == AUDIO_SOURCE_OUTPUT_DEVICE) {
        obs_data_t *settings = create_pulse_device_settings(config->device_id);
        obs_source_t *source =
            obs_source_create("pulse_output_capture", name, settings, NULL);
        obs_data_release(settings);
        return source;
    }

    if (config->kind == AUDIO_SOURCE_INPUT_DEVICE) {
        obs_data_t *settings = create_pulse_device_settings(config->device_id);
        obs_source_t *source =
            obs_source_create("pulse_input_capture", name, settings, NULL);
        obs_data_release(settings);
        return source;
    }

    if (config->kind == AUDIO_SOURCE_SELECTED_INPUT_DEVICE)
        return NULL;

    if (obs_source_get_display_name("pipewire_audio_application_capture")) {
        obs_data_t *settings = obs_data_create();
        if (settings) {
            if (config->match_value[0] != '\0') {
                obs_data_set_string(settings, "TargetName", config->match_value);
                obs_data_set_string(settings, "apps", config->match_value);
            }
            obs_data_set_string(settings, "AppCaptureMode", "Application");
            obs_data_set_string(settings, "CaptureMode", "Application");
            obs_data_set_string(settings, "MatchPriority", "MatchBinaryFirst");
            obs_data_set_string(settings, "MatchPriorty", "MatchBinaryFirst");
        }
        obs_source_t *source = obs_source_create(
            "pipewire_audio_application_capture", name, settings, NULL);
        obs_data_release(settings);
        return source;
    }

    fprintf(stderr,
            "[engine] WARN: app audio source '%s' requested but no app-audio "
            "OBS source is registered\n",
            config->display_name[0] ? config->display_name : config->match_value);
    return NULL;
}

static bool register_audio_source(engine_state_t *state, obs_source_t *source,
                                  const audio_source_config_t *config,
                                  int track, float volume,
                                  bool active_tracks[CLIPPER_MAX_AUDIO_TRACKS])
{
    if (!source || state->audio_source_count >= CLIPPER_MAX_AUDIO_SOURCES)
        return false;

    uint32_t mask = track_mask(track);
    if (mask == 0)
        return false;

    obs_source_set_audio_mixers(source, mask);
    obs_source_set_volume(source, volume);

    size_t idx = state->audio_source_count++;
    state->audio_sources[idx] = source;
    state->audio_source_tracks[idx] = track;
    state->audio_source_kinds[idx] =
        config ? config->kind : AUDIO_SOURCE_OUTPUT_DEVICE;
    obs_set_output_source((uint32_t)(idx + 1), source);
    active_tracks[track - 1] = true;

    if (config &&
        (config->kind == AUDIO_SOURCE_APPLICATION ||
         config->kind == AUDIO_SOURCE_GAME_APP)) {
        fprintf(stderr,
                "[engine] App audio capture configured: track %d; source '%s'; "
                "selector '%s'\n",
                track,
                config->display_name[0] ? config->display_name : "application",
                config->match_value[0] ? config->match_value : "(empty)");
    }

    if (g_verbose) {
        fprintf(stderr,
                "[engine] audio source routed to track %d with mask 0x%x volume %.2f\n",
                track, mask, volume);
    }

    return true;
}

static bool create_single_mix_audio_sources(engine_state_t *state,
                                            bool active_tracks[CLIPPER_MAX_AUDIO_TRACKS])
{
    audio_source_config_t desktop = {
        .kind = AUDIO_SOURCE_OUTPUT_DEVICE,
    };
    copy_string(desktop.backend, sizeof(desktop.backend), "pulse");
    copy_string(desktop.device_id, sizeof(desktop.device_id), "default");
    copy_string(desktop.display_name, sizeof(desktop.display_name), "System Audio");

    obs_source_t *desktop_source =
        create_audio_source_instance(&desktop, state->audio_source_count);
    if (!desktop_source) {
        fprintf(stderr, "[engine] WARN: Failed to create pulse_output_capture\n"
                        "[engine]   Audio will be silent unless another source succeeds\n");
    } else if (!register_audio_source(
                   state, desktop_source, &desktop, 1, 1.0f, active_tracks)) {
        obs_source_release(desktop_source);
    }

    if (!state->mic_enabled)
        return active_tracks[0];

    audio_source_config_t mic = {
        .kind = AUDIO_SOURCE_INPUT_DEVICE,
    };
    copy_string(mic.backend, sizeof(mic.backend), state->mic_backend);
    copy_string(mic.device_id, sizeof(mic.device_id), state->mic_device_id);
    copy_string(mic.display_name, sizeof(mic.display_name), state->mic_display_name);

    obs_source_t *mic_source =
        create_audio_source_instance(&mic, state->audio_source_count);
    if (!mic_source) {
        fprintf(stderr, "[engine] WARN: Failed to create pulse_input_capture for microphone\n");
    } else if (!register_audio_source(
                   state, mic_source, &mic, 1, state->mic_volume, active_tracks)) {
        obs_source_release(mic_source);
    }

    return active_tracks[0];
}

static bool create_split_audio_sources(engine_state_t *state,
                                       bool active_tracks[CLIPPER_MAX_AUDIO_TRACKS])
{
    bool any = false;

    for (int i = 0; i < CLIPPER_MAX_AUDIO_TRACKS; i++) {
        audio_track_config_t *track = &state->audio_tracks[i];
        if (!track->enabled || track->track < 1 ||
            track->track > CLIPPER_MAX_AUDIO_TRACKS)
            continue;

        for (size_t j = 0; j < track->source_count; j++) {
            audio_source_config_t resolved_source = track->sources[j];
            if (resolved_source.kind == AUDIO_SOURCE_SELECTED_INPUT_DEVICE) {
                if (!state->mic_enabled) {
                    active_tracks[track->track - 1] = true;
                    any = true;
                    continue;
                }

                resolved_source.kind = AUDIO_SOURCE_INPUT_DEVICE;
                copy_string(resolved_source.backend,
                            sizeof(resolved_source.backend),
                            state->mic_backend);
                copy_string(resolved_source.device_id,
                            sizeof(resolved_source.device_id),
                            state->mic_device_id);
                copy_string(resolved_source.display_name,
                            sizeof(resolved_source.display_name),
                            state->mic_display_name);
            }

            obs_source_t *source = create_audio_source_instance(
                &resolved_source, state->audio_source_count);
            if (!source) {
                fprintf(stderr,
                        "[engine] WARN: could not create audio source for track %d; "
                        "source '%s'; selector '%s'\n",
                        track->track,
                        resolved_source.display_name[0]
                            ? resolved_source.display_name
                            : audio_source_kind_to_string(resolved_source.kind),
                        resolved_source.match_value[0]
                            ? resolved_source.match_value
                            : "(empty)");
                continue;
            }

            if (register_audio_source(
                    state, source, &resolved_source,
                    track->track, track->volume, active_tracks)) {
                any = true;
            } else {
                obs_source_release(source);
            }
        }
    }

    if (!any)
        fprintf(stderr, "[engine] WARN: split audio mode has no active audio sources\n");

    return any;
}

static bool setup_audio_routing(engine_state_t *state,
                                bool active_tracks[CLIPPER_MAX_AUDIO_TRACKS])
{
    memset(active_tracks, 0, sizeof(bool) * CLIPPER_MAX_AUDIO_TRACKS);
    memset(state->audio_source_tracks, 0, sizeof(state->audio_source_tracks));
    memset(state->audio_source_kinds, 0, sizeof(state->audio_source_kinds));
    state->audio_source_count = 0;

    if (state->audio_mode == AUDIO_MODE_SPLIT_TRACKS)
        return create_split_audio_sources(state, active_tracks);

    return create_single_mix_audio_sources(state, active_tracks);
}

static bool setup_audio_encoder_for_track(engine_state_t *state, int track_index)
{
    obs_data_t *aenc_settings = create_audio_encoder_settings(
        state->audio_encoder, state->audio_bitrate);
    char name[64];
    snprintf(name, sizeof(name), "clipper_aenc_%d", track_index + 1);
    state->aenc[track_index] = obs_audio_encoder_create(
        state->audio_encoder, name, aenc_settings, (size_t)track_index, NULL);
    obs_data_release(aenc_settings);

    if (!state->aenc[track_index]) {
        fprintf(stderr, "[engine] FAIL: obs_audio_encoder_create(%s) for track %d\n",
                state->audio_encoder, track_index + 1);
        return false;
    }

    obs_encoder_set_audio(state->aenc[track_index], obs_get_audio());
    obs_output_set_audio_encoder(
        state->replay_output, state->aenc[track_index], (size_t)track_index);
    return true;
}

static bool setup_audio_encoders(engine_state_t *state,
                                 const bool active_tracks[CLIPPER_MAX_AUDIO_TRACKS])
{
    bool any = false;
    for (int i = 0; i < CLIPPER_MAX_AUDIO_TRACKS; i++) {
        if (!active_tracks[i])
            continue;

        if (!setup_audio_encoder_for_track(state, i))
            return false;
        any = true;
    }

    if (!any) {
        fprintf(stderr,
                "[engine] WARN: no active audio tracks; creating silent track 1 "
                "encoder so replay output can start\n");
        return setup_audio_encoder_for_track(state, 0);
    }

    return true;
}

/* Callback to remove scene items during cleanup */
static bool remove_sceneitem_callback(obs_scene_t *scene, obs_sceneitem_t *item, void *param)
{
    (void)scene;
    (void)param;
    obs_sceneitem_remove(item);
    return true; /* continue enumeration */
}

static bool attach_video_source_to_scene(engine_state_t *state, obs_source_t *source)
{
    if (!source)
        return false;

    if (!state->scene)
        state->scene = obs_scene_create("clipper_scene");

    if (!state->scene) {
        fprintf(stderr, "[engine] WARN: Failed to create clipper_scene\n");
        obs_set_output_source(0, source);
        return true;
    }

    obs_sceneitem_t *item = obs_scene_add(state->scene, source);
    if (item) {
        struct vec2 bounds;
        vec2_set(&bounds, (float)state->output_width, (float)state->output_height);
        obs_sceneitem_set_bounds_type(item, OBS_BOUNDS_SCALE_INNER);
        obs_sceneitem_set_bounds(item, &bounds);
    }

    obs_set_output_source(0, obs_scene_get_source(state->scene));
    return true;
}

static const char *env_or_default(const char *name, const char *fallback)
{
    const char *value = getenv(name);
    return (value && *value) ? value : fallback;
}

static bool running_in_flatpak(void)
{
    const char *flatpak_id = getenv("FLATPAK_ID");
    return flatpak_id && *flatpak_id;
}

static void build_obs_paths(char *plugin_dir, size_t plugin_dir_size,
                            char *data_dir, size_t data_dir_size,
                            char *plugin_data_pattern,
                            size_t plugin_data_pattern_size)
{
    const char *libdir = env_or_default("CLIPPER_OBS_LIBDIR", OBS_LIBDIR_DEFAULT);
    const char *datadir = env_or_default("CLIPPER_OBS_DATADIR", OBS_DATADIR_DEFAULT);

    snprintf(plugin_dir, plugin_dir_size, "%s/obs-plugins", libdir);
    snprintf(data_dir, data_dir_size, "%s", datadir);
    snprintf(plugin_data_pattern, plugin_data_pattern_size,
             "%s/obs-plugins/%%module%%", datadir);
}

static void build_plugin_path(char *dest, size_t size,
                              const char *plugin_dir, const char *module_name)
{
    snprintf(dest, size, "%s/%s", plugin_dir, module_name);
}

static void try_load_pipewire_audio_plugin_from_file(const char *binary_path,
                                                     const char *data_path)
{
    if (!binary_path || !*binary_path)
        return;

    if (access(binary_path, R_OK) != 0)
        return;

    obs_module_t *mod = NULL;
    int r = obs_open_module(&mod, binary_path, data_path);
    if (r != MODULE_SUCCESS) {
        fprintf(stderr,
                "[engine] WARN: obs_open_module(linux-pipewire-audio) -> %d\n"
                "[engine]   Plugin path: %s\n",
                r, binary_path);
        return;
    }

    if (!obs_init_module(mod)) {
        fprintf(stderr, "[engine] WARN: obs_init_module(linux-pipewire-audio) failed\n");
        return;
    }

    if (g_verbose)
        fprintf(stderr, "[engine] loaded: %s\n", binary_path);
}

static void try_load_pipewire_audio_plugin_from_obs_user_dir(const char *base_dir)
{
    if (!base_dir || !*base_dir)
        return;

    char binary_path[PATH_MAX];
    char data_path[PATH_MAX];
    snprintf(binary_path, sizeof(binary_path),
             "%s/bin/64bit/linux-pipewire-audio.so", base_dir);
    snprintf(data_path, sizeof(data_path), "%s/data", base_dir);
    try_load_pipewire_audio_plugin_from_file(binary_path, data_path);
}

static void try_load_pipewire_audio_plugin(void)
{
    const char *explicit_path = getenv("CLIPPER_PIPEWIRE_AUDIO_PLUGIN");
    const char *explicit_data = getenv("CLIPPER_PIPEWIRE_AUDIO_PLUGIN_DATA");
    if (explicit_path && *explicit_path) {
        try_load_pipewire_audio_plugin_from_file(
            explicit_path, explicit_data && *explicit_data ? explicit_data : NULL);
    }

    if (obs_source_get_display_name("pipewire_audio_application_capture"))
        return;

    try_load_pipewire_audio_plugin_from_file(
        PIPEWIRE_AUDIO_PLUGIN_DEFAULT,
        OBS_DATADIR_DEFAULT "/obs-plugins/linux-pipewire-audio");

    if (obs_source_get_display_name("pipewire_audio_application_capture"))
        return;

    if (running_in_flatpak())
        return;

    const char *home = getenv("HOME");
    if (!home || !*home)
        return;

    char user_plugin[PATH_MAX];
    snprintf(
        user_plugin,
        sizeof(user_plugin),
        "%s/.config/obs-studio/plugins/linux-pipewire-audio",
        home);
    try_load_pipewire_audio_plugin_from_obs_user_dir(user_plugin);
}

/* ── libobs initialisation ───────────────────────────────────────────────── */
static void configure_obs_nix_platform(void)
{
    const char *wayland_display = getenv("WAYLAND_DISPLAY");
    const char *session_type = getenv("XDG_SESSION_TYPE");

    if ((wayland_display && *wayland_display) ||
        (session_type && strcmp(session_type, "wayland") == 0)) {
        if (!g_obs_wayland_display)
            g_obs_wayland_display = wl_display_connect(NULL);
        if (g_obs_wayland_display) {
            obs_set_nix_platform(OBS_NIX_PLATFORM_WAYLAND);
            obs_set_nix_platform_display(g_obs_wayland_display);
            if (g_verbose)
                fprintf(stderr, "[engine] OBS platform: Wayland\n");
            return;
        }
        fprintf(stderr,
                "[engine] WARN: could not connect to Wayland display; "
                "falling back to X11/EGL\n");
    }

    obs_set_nix_platform(OBS_NIX_PLATFORM_X11_EGL);
    if (g_verbose)
        fprintf(stderr, "[engine] OBS platform: X11/EGL\n");
}

static const char *display_capture_source_id(void)
{
    if (obs_get_nix_platform() == OBS_NIX_PLATFORM_X11_EGL &&
        obs_source_get_display_name("xshm_input"))
        return "xshm_input";

    if (obs_source_get_display_name("pipewire-screen-capture-source"))
        return "pipewire-screen-capture-source";

    return NULL;
}

static void clear_obs_nix_platform(void)
{
    obs_set_nix_platform_display(NULL);
    if (g_obs_wayland_display) {
        wl_display_disconnect(g_obs_wayland_display);
        g_obs_wayland_display = NULL;
    }
}

static bool libobs_init(engine_state_t *state)
{
    char obs_plugin_dir[PATH_MAX];
    char obs_data_dir[PATH_MAX];
    char obs_plugin_data_pattern[PATH_MAX];
    char obs_libobs_data[PATH_MAX * 2];
    char plugin_paths[6][PATH_MAX];
    char plugin_data_paths[6][PATH_MAX * 2];
    char vkc_plugin_so[PATH_MAX];
    char vkc_plugin_data[PATH_MAX];
    static const char *plugin_names[] = {
        "obs-ffmpeg",
        "obs-x264",
        "obs-outputs",
        "linux-pulseaudio",
        "linux-pipewire",
        "linux-capture",
    };

    build_obs_paths(obs_plugin_dir, sizeof(obs_plugin_dir),
                    obs_data_dir, sizeof(obs_data_dir),
                    obs_plugin_data_pattern, sizeof(obs_plugin_data_pattern));
    snprintf(obs_libobs_data, sizeof(obs_libobs_data), "%s/libobs/", obs_data_dir);
    for (size_t i = 0; i < sizeof(plugin_names) / sizeof(plugin_names[0]); i++) {
        char module_name[PATH_MAX];
        snprintf(module_name, sizeof(module_name), "%s.so", plugin_names[i]);
        build_plugin_path(plugin_paths[i], sizeof(plugin_paths[i]),
                          obs_plugin_dir, module_name);
        snprintf(plugin_data_paths[i], sizeof(plugin_data_paths[i]),
                 "%s/obs-plugins/%s", obs_data_dir, plugin_names[i]);
    }
    snprintf(vkc_plugin_so, sizeof(vkc_plugin_so), "%s",
             env_or_default("CLIPPER_VKCAPTURE_PLUGIN", VKC_PLUGIN_SO_DEFAULT));
    snprintf(vkc_plugin_data, sizeof(vkc_plugin_data), "%s",
             env_or_default("CLIPPER_VKCAPTURE_PLUGIN_DATA", VKC_PLUGIN_DATA_DEFAULT));

    /* Install our log handler before obs_startup so we capture early messages */
    base_set_log_handler(engine_log, NULL);
    configure_obs_nix_platform();

    /* 1. Start up libobs */
    if (!obs_startup("en-US", NULL, NULL)) {
        fprintf(stderr, "[engine] FAIL: obs_startup\n");
        return false;
    }

    /* 2. Core data path (shaders/effects) — must be set before obs_reset_video */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    obs_add_data_path(obs_libobs_data);
#pragma GCC diagnostic pop

    /* 3. Plugin module paths */
    obs_add_module_path(obs_plugin_dir, obs_plugin_data_pattern);

    /* 4. Reset video — required even headlessly. */
    struct obs_video_info ovi = {
        .graphics_module = "libobs-opengl",
        .fps_num         = (unsigned int)state->fps,
        .fps_den         = 1,
        .base_width      = (unsigned int)state->base_width,
        .base_height     = (unsigned int)state->base_height,
        .output_width    = (unsigned int)state->output_width,
        .output_height   = (unsigned int)state->output_height,
        .output_format   = VIDEO_FORMAT_NV12,
        .adapter         = 0,
        .gpu_conversion  = true,
        .colorspace      = VIDEO_CS_709,
        .range           = VIDEO_RANGE_FULL,
        .scale_type      = OBS_SCALE_BILINEAR,
    };
    int vr = obs_reset_video(&ovi);
    if (vr != OBS_VIDEO_SUCCESS) {
        fprintf(stderr, "[engine] FAIL: obs_reset_video -> %d\n", vr);
        obs_shutdown();
        return false;
    }
    if (g_verbose)
        fprintf(stderr,
                "[engine] video pipeline: NV12 Rec.709 full range SDR "
                "(SDR white %.0f nits, HDR peak %.0f nits)\n",
                obs_get_video_sdr_white_level(),
                obs_get_video_hdr_nominal_peak_level());

    /* 5. Reset audio */
    struct obs_audio_info oai = {
        .samples_per_sec = 44100,
        .speakers        = SPEAKERS_STEREO,
    };
    if (!obs_reset_audio(&oai)) {
        fprintf(stderr, "[engine] FAIL: obs_reset_audio\n");
        obs_shutdown();
        return false;
    }

    /* 6. Load only the plugins we need.
     *    obs_load_all_modules() crashes headlessly because obs-websocket and
     *    obs-browser dereference obs_frontend_* pointers that are NULL without
     *    a frontend.  Use obs_open_module + obs_init_module selectively. */
    for (size_t i = 0; i < sizeof(plugin_paths) / sizeof(plugin_paths[0]); i++) {
        obs_module_t *mod = NULL;
        int r = obs_open_module(&mod, plugin_paths[i], plugin_data_paths[i]);
        if (r != MODULE_SUCCESS) {
            fprintf(stderr, "[engine] WARN: obs_open_module(%s) -> %d (skipping)\n",
                    plugin_paths[i], r);
        } else {
            if (!obs_init_module(mod))
                fprintf(stderr, "[engine] WARN: obs_init_module(%s) failed\n",
                        plugin_paths[i]);
            else if (g_verbose)
                fprintf(stderr, "[engine] loaded: %s\n", plugin_paths[i]);
        }
    }

    /* linux-vkcapture.so — loaded by full path only for game capture mode. */
    if (state->capture_mode == CAPTURE_MODE_GAME) {
        obs_module_t *vkc_mod = NULL;
        int r = obs_open_module(&vkc_mod, vkc_plugin_so, vkc_plugin_data);
        if (r != MODULE_SUCCESS) {
            fprintf(stderr,
                    "[engine] WARN: obs_open_module(linux-vkcapture) -> %d\n"
                    "[engine]   Expected path: %s\n"
                    "[engine]   Game capture is advanced mode; display capture remains available.\n",
                    r, vkc_plugin_so);
        } else {
            bool ok = obs_init_module(vkc_mod);
            if (!ok) {
                fprintf(stderr, "[engine] WARN: obs_init_module(linux-vkcapture) returned false\n");
            } else if (g_verbose) {
                fprintf(stderr, "[engine] loaded: linux-vkcapture\n");
            }
        }
    } else if (g_verbose) {
        fprintf(stderr, "[engine] skipping linux-vkcapture in display capture mode\n");
    }

    try_load_pipewire_audio_plugin();
    obs_post_load_modules();

    bool vkcapture_registered = obs_source_get_display_name("vkcapture-source") != NULL;
    const char *display_source_id = display_capture_source_id();
    if (state->capture_mode == CAPTURE_MODE_DISPLAY && display_source_id &&
        strcmp(display_source_id, "pipewire-screen-capture-source") == 0)
        subscribe_display_portal_response(state);

    if (!vkcapture_registered && state->capture_mode == CAPTURE_MODE_GAME) {
        fprintf(stderr,
                "[engine] WARN: vkcapture-source type is not registered\n"
                "[engine]   Plugin path: %s\n"
                "[engine]   Plugin data path: %s\n",
                vkc_plugin_so, vkc_plugin_data);
    } else if (vkcapture_registered && g_verbose) {
        fprintf(stderr, "[engine] vkcapture-source type registered\n");
    }

    if (!display_source_id) {
        fprintf(stderr,
                "[engine] WARN: no display capture source type is registered\n"
                "[engine]   Display capture mode will be unavailable\n");
    } else if (g_verbose) {
        fprintf(stderr, "[engine] display capture source registered: %s\n",
                display_source_id);
    }

    /* 6½. Create the selected video source inside a scene.
     * Regular OBS routes capture sources through the scene compositor; doing
     * the same here keeps scaling, conversion, and frame clearing consistent. */
    if (state->capture_mode == CAPTURE_MODE_GAME) {
        obs_data_t *vkc_settings = obs_data_create();
        obs_data_set_bool(vkc_settings, "show_cursor", false);
        obs_data_set_bool(vkc_settings, "allow_transparency", false);
        obs_data_set_bool(vkc_settings, "force_hdr", false);
        state->vkc_source = obs_source_create(
            "vkcapture-source", "game_capture", vkc_settings, NULL);
        obs_data_release(vkc_settings);
        if (!state->vkc_source) {
            fprintf(stderr,
                    "[engine] WARN: Failed to create vkcapture-source "
                    "(plugin may not be loaded)\n");
        } else {
            attach_video_source_to_scene(state, state->vkc_source);
            if (g_verbose)
                fprintf(stderr,
                        "[engine] vkcapture source created in scene on output channel 0\n");
        }
    } else {
        obs_data_t *display_settings = obs_data_create();
        obs_data_set_bool(display_settings, "ShowCursor", false);
        obs_data_set_bool(display_settings, "show_cursor", false);
        if (display_source_id && strcmp(display_source_id, "xshm_input") == 0)
            obs_data_set_int(display_settings, "screen", 0);
        if (display_source_id &&
            strcmp(display_source_id, "pipewire-screen-capture-source") == 0 &&
            state->pipewire_restore_token[0] != '\0')
            obs_data_set_string(
                display_settings, "RestoreToken", state->pipewire_restore_token);
        state->display_source = display_source_id ? obs_source_create(
            display_source_id, "screen_capture", display_settings, NULL) : NULL;
        obs_data_release(display_settings);
        if (!state->display_source) {
            fprintf(stderr,
                    "[engine] WARN: Failed to create display capture source\n");
        } else {
            attach_video_source_to_scene(state, state->display_source);
            if (g_verbose)
                fprintf(stderr,
                        "[engine] %s display source created in scene on output channel 0\n",
                        display_source_id);
        }
    }

    /* 6¾. Create configured audio sources and route them to OBS audio mixers. */
    bool active_audio_tracks[CLIPPER_MAX_AUDIO_TRACKS] = {0};
    setup_audio_routing(state, active_audio_tracks);

    /* Initialize hook tracking */
    state->game_hooked = false;
    state->game_exe[0] = '\0';
    state->logged_missing_hook_proc = false;
    state->logged_hook_poll = false;
    clock_gettime(CLOCK_MONOTONIC, &state->last_hook_check);

    /* 7. Create replay buffer output (created once; started/stopped on demand) */
    obs_data_t *settings = obs_data_create();
    obs_data_set_string(settings, "directory",    state->output_dir);
    obs_data_set_string(settings, "format",       "%CCYY-%MM-%DD_%hh-%mm-%ss");
    obs_data_set_string(settings, "extension",    state->output_format);
    obs_data_set_int(settings,    "max_time_sec", state->max_time_sec);
    obs_data_set_int(settings,    "max_size_mb",  state->max_size_mb);

    state->replay_output = obs_output_create(
        "replay_buffer", "clipper_replay", settings, NULL);
    obs_data_release(settings);

    if (!state->replay_output) {
        fprintf(stderr, "[engine] FAIL: obs_output_create(replay_buffer)\n");
        obs_shutdown();
        return false;
    }

    /* 8. Video encoder — create with configured encoder and quality settings */
    obs_data_t *venc_settings = create_video_encoder_settings(
        state->video_encoder, state->video_rate_control, state->quality_cqp,
        state->video_bitrate, state->video_max_bitrate, state->vaapi_device);
    state->venc = obs_video_encoder_create(
        state->video_encoder, "clipper_venc", venc_settings, NULL);
    obs_data_release(venc_settings);
    
    if (!state->venc) {
        fprintf(stderr, "[engine] FAIL: obs_video_encoder_create(%s)\n",
                state->video_encoder);
        obs_output_release(state->replay_output);
        obs_shutdown();
        return false;
    }
    obs_encoder_set_video(state->venc, obs_get_video());

    /* 9. Audio encoders — one encoder per active OBS audio mix/track. */
    if (!setup_audio_encoders(state, active_audio_tracks)) {
        obs_encoder_release(state->venc);
        obs_output_release(state->replay_output);
        obs_shutdown();
        return false;
    }

    /* 10. Attach encoders to output */
    obs_output_set_video_encoder(state->replay_output, state->venc);

    /* 11. Connect "saved" signal BEFORE starting the output, so the
     *     first save is not missed. */
    signal_handler_t *sh = obs_output_get_signal_handler(state->replay_output);
    signal_handler_connect(sh, "saved", on_clip_saved, state);

    fprintf(stderr, "[engine] libobs initialised OK\n");
    return true;
}

/* ── libobs shutdown ─────────────────────────────────────────────────────── */
static void libobs_shutdown(engine_state_t *state)
{
    unsubscribe_display_portal_response(state);
    if (state->buffer_active && state->replay_output) {
        obs_output_stop(state->replay_output);
        state->buffer_active = false;
    }
    if (state->replay_output) {
        obs_output_release(state->replay_output);
        state->replay_output = NULL;
    }
    if (state->venc) {
        obs_encoder_release(state->venc);
        state->venc = NULL;
    }
    for (int i = 0; i < CLIPPER_MAX_AUDIO_TRACKS; i++) {
        if (state->aenc[i]) {
            obs_encoder_release(state->aenc[i]);
            state->aenc[i] = NULL;
        }
    }
    
    /* Clear output sources first */
    obs_set_output_source(0, NULL);
    for (size_t i = 0; i < state->audio_source_count; i++)
        obs_set_output_source((uint32_t)(i + 1), NULL);
    
    /* Remove all scene items before destroying the scene */
    if (state->scene) {
        obs_scene_enum_items(state->scene, remove_sceneitem_callback, NULL);
    }
    
    /* Now release sources - scene items no longer hold references */
    if (state->vkc_source) {
        obs_source_release(state->vkc_source);
        state->vkc_source = NULL;
    }
    for (size_t i = 0; i < state->audio_source_count; i++) {
        if (state->audio_sources[i]) {
            obs_source_release(state->audio_sources[i]);
            state->audio_sources[i] = NULL;
        }
    }
    state->audio_source_count = 0;
    memset(state->audio_source_tracks, 0, sizeof(state->audio_source_tracks));
    memset(state->audio_source_kinds, 0, sizeof(state->audio_source_kinds));
    if (state->display_source) {
        obs_source_release(state->display_source);
        state->display_source = NULL;
    }
    
    /* Finally release the scene itself */
    if (state->scene) {
        obs_scene_release(state->scene);
        state->scene = NULL;
    }

    obs_enter_graphics();
    destroy_preview_resources_locked(state);
    obs_leave_graphics();
    
    obs_shutdown();
    clear_obs_nix_platform();
    fprintf(stderr, "[engine] obs_shutdown complete\n");
}

/* ── IPC server setup ────────────────────────────────────────────────────── */
static int ipc_server_create(const char *socket_path)
{
    /* Remove any stale socket file */
    unlink(socket_path);

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("[engine] socket");
        return -1;
    }

    /* Non-blocking so poll() drives everything */
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl >= 0) fcntl(fd, F_SETFL, fl | O_NONBLOCK);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("[engine] bind");
        close(fd);
        return -1;
    }
    if (listen(fd, 8) < 0) {
        perror("[engine] listen");
        close(fd);
        return -1;
    }
    return fd;
}

/* ── Configuration ────────────────────────────────────────────────────────── */

/**
 * Set default configuration values.
 */
static void apply_config_defaults(engine_state_t *state)
{
    const char *home = getenv("HOME");
    snprintf(state->output_dir, sizeof(state->output_dir),
             "%s/Videos/Clipper", home ? home : "/tmp");
    
    state->max_time_sec = 60;
    state->max_size_mb = 1024;
    state->fps = 60;
    state->base_width = 1920;
    state->base_height = 1080;
    state->output_width = 1920;
    state->output_height = 1080;
    strncpy(state->output_format, "mkv", sizeof(state->output_format) - 1);
    strncpy(state->video_encoder, "obs_x264", sizeof(state->video_encoder) - 1);
    strncpy(state->audio_encoder, "ffmpeg_aac", sizeof(state->audio_encoder) - 1);
    state->video_rate_control = VIDEO_RATE_CONTROL_CQP;
    state->quality_cqp = 23;
    state->video_bitrate = 12000;
    state->video_max_bitrate = 20000;
    state->audio_bitrate = 192;
    strncpy(state->vaapi_device, "auto", sizeof(state->vaapi_device) - 1);
    state->capture_mode = CAPTURE_MODE_DISPLAY;
    state->pipewire_restore_token[0] = '\0';
    state->audio_mode = AUDIO_MODE_SINGLE_MIX;
    state->mic_enabled = false;
    copy_string(state->mic_backend, sizeof(state->mic_backend), "pulse");
    copy_string(state->mic_device_id, sizeof(state->mic_device_id), "default");
    copy_string(state->mic_display_name, sizeof(state->mic_display_name), "Default");
    state->mic_volume = 1.0f;
    for (int i = 0; i < CLIPPER_MAX_AUDIO_TRACKS; i++) {
        state->audio_tracks[i].track = i + 1;
        state->audio_tracks[i].enabled = false;
        state->audio_tracks[i].volume = 1.0f;
        snprintf(state->audio_tracks[i].label,
                 sizeof(state->audio_tracks[i].label), "Track %d", i + 1);
        state->audio_tracks[i].source_count = 0;
    }
    clock_gettime(CLOCK_MONOTONIC, &state->last_restore_token_check);
}

/**
 * Expand ~ in paths using wordexp().
 * Returns 0 on success, -1 on failure.
 */
static int expand_path(const char *input, char *output, size_t output_size)
{
    if (!input || !output || output_size == 0)
        return -1;
    
    /* If no ~ to expand, just copy */
    if (input[0] != '~') {
        strncpy(output, input, output_size - 1);
        output[output_size - 1] = '\0';
        return 0;
    }
    
    wordexp_t exp;
    int ret = wordexp(input, &exp, WRDE_NOCMD);
    if (ret != 0) {
        fprintf(stderr, "[engine] WARN: wordexp failed for path '%s'\n", input);
        strncpy(output, input, output_size - 1);
        output[output_size - 1] = '\0';
        return -1;
    }
    
    if (exp.we_wordc > 0) {
        strncpy(output, exp.we_wordv[0], output_size - 1);
        output[output_size - 1] = '\0';
    } else {
        strncpy(output, input, output_size - 1);
        output[output_size - 1] = '\0';
    }
    
    wordfree(&exp);
    return 0;
}

/**
 * Map UI encoder names to OBS encoder IDs.
 */
static const char *map_encoder_name(const char *ui_name, bool is_video)
{
    if (is_video) {
        /* x264 variants */
        if (strcmp(ui_name, "libx264") == 0 || strcmp(ui_name, "x264") == 0)
            return "obs_x264";
        /* VAAPI variants (old and new names) */
        if (strcmp(ui_name, "h264_vaapi") == 0 || strcmp(ui_name, "vaapi_h264") == 0 ||
            strcmp(ui_name, "hevc_vaapi") == 0 || strcmp(ui_name, "vaapi_hevc") == 0 ||
            strcmp(ui_name, "av1_vaapi") == 0 || strcmp(ui_name, "vaapi_av1") == 0 ||
            strcmp(ui_name, "ffmpeg_vaapi") == 0)
            return "ffmpeg_vaapi";
        /* Already an OBS encoder ID */
        return ui_name;
    } else {
        /* Audio encoder mapping (old and new names) */
        if (strcmp(ui_name, "aac") == 0)
            return "ffmpeg_aac";
        if (strcmp(ui_name, "opus") == 0)
            return "ffmpeg_opus";
        if (strcmp(ui_name, "flac16") == 0)
            return "ffmpeg_flac";
        if (strcmp(ui_name, "pcm16") == 0)
            return "ffmpeg_pcm_s16le";
        if (strcmp(ui_name, "pcm24") == 0)
            return "ffmpeg_pcm_s24le";
        if (strcmp(ui_name, "pcm32f") == 0)
            return "ffmpeg_pcm_f32le";
        if (strcmp(ui_name, "alac24") == 0)
            return "ffmpeg_alac";
        if (strcmp(ui_name, "fdk_aac") == 0 || strcmp(ui_name, "fdk_he_aac") == 0)
            return "libfdk_aac";
        /* Already an OBS encoder ID */
        return ui_name;
    }
}

/**
 * Parse resolution string like "1920x1080" into width and height.
 * Returns 0 on success, -1 on failure.
 */
static int parse_resolution(const char *res_str, int *width, int *height)
{
    if (!res_str || !width || !height)
        return -1;
    
    char *endptr;
    long w = strtol(res_str, &endptr, 10);
    if (*endptr != 'x' && *endptr != 'X')
        return -1;
    
    long h = strtol(endptr + 1, &endptr, 10);
    if (*endptr != '\0')
        return -1;
    
    if (w <= 0 || h <= 0 || w > 7680 || h > 4320)
        return -1;
    
    *width = (int)w;
    *height = (int)h;
    return 0;
}

static void parse_audio_source_config(cJSON *item, audio_source_config_t *source)
{
    memset(source, 0, sizeof(*source));
    copy_string(source->backend, sizeof(source->backend), "pulse");
    copy_string(source->device_id, sizeof(source->device_id), "default");
    copy_string(source->display_name, sizeof(source->display_name), "Audio Source");

    cJSON *kind_item = cJSON_GetObjectItem(item, "kind");
    if (!kind_item || !cJSON_IsString(kind_item) ||
        !audio_source_kind_from_string(kind_item->valuestring, &source->kind)) {
        source->kind = AUDIO_SOURCE_OUTPUT_DEVICE;
    }

    cJSON *string_item = cJSON_GetObjectItem(item, "backend");
    if (string_item && cJSON_IsString(string_item))
        copy_string(source->backend, sizeof(source->backend), string_item->valuestring);

    string_item = cJSON_GetObjectItem(item, "device_id");
    if (string_item && cJSON_IsString(string_item))
        copy_string(source->device_id, sizeof(source->device_id), string_item->valuestring);

    string_item = cJSON_GetObjectItem(item, "display_name");
    if (string_item && cJSON_IsString(string_item))
        copy_string(source->display_name,
                    sizeof(source->display_name), string_item->valuestring);

    cJSON *match = cJSON_GetObjectItem(item, "match");
    if (match && cJSON_IsObject(match)) {
        string_item = cJSON_GetObjectItem(match, "type");
        if (string_item && cJSON_IsString(string_item))
            copy_string(source->match_type,
                        sizeof(source->match_type), string_item->valuestring);

        string_item = cJSON_GetObjectItem(match, "value");
        if (string_item && cJSON_IsString(string_item))
            copy_string(source->match_value,
                        sizeof(source->match_value), string_item->valuestring);
    }
}

static void parse_audio_config(engine_state_t *state, cJSON *audio)
{
    if (!audio || !cJSON_IsObject(audio))
        return;

    cJSON *item = cJSON_GetObjectItem(audio, "mode");
    if (item && cJSON_IsString(item) && !set_audio_mode_from_string(state, item->valuestring)) {
        fprintf(stderr, "[engine] WARN: invalid audio.mode '%s'; using single_mix\n",
                item->valuestring);
    }

    cJSON *mic = cJSON_GetObjectItem(audio, "microphone");
    if (mic && cJSON_IsObject(mic)) {
        item = cJSON_GetObjectItem(mic, "enabled");
        if (item && cJSON_IsBool(item))
            state->mic_enabled = cJSON_IsTrue(item);

        item = cJSON_GetObjectItem(mic, "backend");
        if (item && cJSON_IsString(item))
            copy_string(state->mic_backend, sizeof(state->mic_backend), item->valuestring);

        item = cJSON_GetObjectItem(mic, "device_id");
        if (item && cJSON_IsString(item))
            copy_string(state->mic_device_id,
                        sizeof(state->mic_device_id), item->valuestring);

        item = cJSON_GetObjectItem(mic, "display_name");
        if (item && cJSON_IsString(item))
            copy_string(state->mic_display_name,
                        sizeof(state->mic_display_name), item->valuestring);

        item = cJSON_GetObjectItem(mic, "volume");
        if (item && cJSON_IsNumber(item))
            state->mic_volume = clamp_audio_volume(item->valuedouble);
    }

    cJSON *tracks = cJSON_GetObjectItem(audio, "tracks");
    if (!tracks || !cJSON_IsArray(tracks))
        return;

    cJSON *track_item = NULL;
    cJSON_ArrayForEach(track_item, tracks) {
        if (!cJSON_IsObject(track_item))
            continue;

        item = cJSON_GetObjectItem(track_item, "track");
        if (!item || !cJSON_IsNumber(item))
            continue;

        int track_number = item->valueint;
        if (track_number < 1 || track_number > CLIPPER_MAX_AUDIO_TRACKS)
            continue;

        audio_track_config_t *track = &state->audio_tracks[track_number - 1];
        track->track = track_number;
        track->source_count = 0;

        item = cJSON_GetObjectItem(track_item, "enabled");
        if (item && cJSON_IsBool(item))
            track->enabled = cJSON_IsTrue(item);

        item = cJSON_GetObjectItem(track_item, "volume");
        if (item && cJSON_IsNumber(item))
            track->volume = clamp_audio_volume(item->valuedouble);

        item = cJSON_GetObjectItem(track_item, "label");
        if (item && cJSON_IsString(item))
            copy_string(track->label, sizeof(track->label), item->valuestring);

        cJSON *sources = cJSON_GetObjectItem(track_item, "sources");
        if (!sources || !cJSON_IsArray(sources))
            continue;

        cJSON *source_item = NULL;
        cJSON_ArrayForEach(source_item, sources) {
            if (!cJSON_IsObject(source_item) ||
                track->source_count >= CLIPPER_MAX_AUDIO_SOURCES)
                continue;

            parse_audio_source_config(
                source_item, &track->sources[track->source_count]);
            track->source_count++;
        }
    }
}

/**
 * Load configuration from $XDG_CONFIG_HOME/clipper/config.json, falling back
 * to ~/.config/clipper/config.json when XDG_CONFIG_HOME is unset.
 * Gracefully handles missing file or invalid JSON.
 */
typedef enum {
    CONFIG_LOAD_DEFAULTS_MISSING,
    CONFIG_LOAD_LOADED,
    CONFIG_LOAD_DEFAULTS_ERROR,
} config_load_result_t;

static config_load_result_t load_config_file(engine_state_t *state)
{
    char config_path[PATH_MAX];
    if (!get_config_path(config_path, sizeof(config_path))) {
        fprintf(stderr, "[engine] WARN: no config home set, skipping config file\n");
        return CONFIG_LOAD_DEFAULTS_ERROR;
    }

    /* Try to open the config file */
    FILE *f = fopen(config_path, "r");
    if (!f) {
        int open_error = errno;
        if (open_error != ENOENT) {
            fprintf(stderr, "[engine] WARN: could not open config file '%s': %s\n",
                    config_path, strerror(open_error));
        }
        return open_error == ENOENT ? CONFIG_LOAD_DEFAULTS_MISSING
                                    : CONFIG_LOAD_DEFAULTS_ERROR;
    }
    
    /* Read entire file into buffer */
    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);
    
    if (fsize <= 0 || fsize > 1024*1024) {  /* Sanity check: max 1MB */
        fprintf(stderr, "[engine] WARN: config file size invalid or too large\n");
        fclose(f);
        return CONFIG_LOAD_DEFAULTS_ERROR;
    }
    
    char *json_str = malloc((size_t)fsize + 1);
    if (!json_str) {
        fprintf(stderr, "[engine] WARN: could not allocate memory for config file\n");
        fclose(f);
        return CONFIG_LOAD_DEFAULTS_ERROR;
    }
    
    size_t nread = fread(json_str, 1, (size_t)fsize, f);
    json_str[nread] = '\0';
    fclose(f);
    
    /* Parse JSON */
    cJSON *root = cJSON_Parse(json_str);
    free(json_str);
    
    if (!root || !cJSON_IsObject(root)) {
        const char *err = cJSON_GetErrorPtr();
        fprintf(stderr, "[engine] WARN: failed to parse config file: %s\n",
                err ? err : "root is not an object");
        cJSON_Delete(root);
        return CONFIG_LOAD_DEFAULTS_ERROR;
    }
    
    if (g_verbose)
        fprintf(stderr, "[engine] loaded config from %s\n", config_path);
    
    /* Extract configuration values */
    cJSON *item;

    /* capture_mode */
    item = cJSON_GetObjectItem(root, "capture_mode");
    if (item && cJSON_IsString(item)) {
        if (!set_capture_mode_from_string(state, item->valuestring)) {
            fprintf(stderr,
                    "[engine] WARN: invalid capture_mode '%s'; using display_capture\n",
                    item->valuestring);
        }
    }

    /* pipewire_restore_token */
    item = cJSON_GetObjectItem(root, "pipewire_restore_token");
    if (item && cJSON_IsString(item)) {
        copy_string(state->pipewire_restore_token,
                    sizeof(state->pipewire_restore_token), item->valuestring);
    }
    
    /* replay_buffer_seconds or replay_buffer_length */
    item = cJSON_GetObjectItem(root, "replay_buffer_seconds");
    if (!item)
        item = cJSON_GetObjectItem(root, "replay_buffer_length");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v > 0 && v <= 300)  /* Reasonable range: 1-300 seconds */
            state->max_time_sec = v;
    }

    /* replay_buffer_size_mb */
    item = cJSON_GetObjectItem(root, "replay_buffer_size_mb");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v >= 128 && v <= 8192)
            state->max_size_mb = v;
    }
    
    /* fps */
    item = cJSON_GetObjectItem(root, "fps");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v > 0 && v <= 240)  /* Reasonable range */
            state->fps = v;
    }
    
    /* scaled_resolution or resolution */
    item = cJSON_GetObjectItem(root, "scaled_resolution");
    if (!item)
        item = cJSON_GetObjectItem(root, "resolution");
    if (item && cJSON_IsString(item)) {
        int w, h;
        if (parse_resolution(item->valuestring, &w, &h) == 0) {
            state->output_width = w;
            state->output_height = h;
            /* Also use as base resolution for now */
            state->base_width = w;
            state->base_height = h;
        }
    }
    
    /* output_format or format */
    item = cJSON_GetObjectItem(root, "output_format");
    if (!item)
        item = cJSON_GetObjectItem(root, "format");
    if (item && cJSON_IsString(item)) {
        if (is_supported_output_format(item->valuestring)) {
            strncpy(state->output_format, item->valuestring,
                    sizeof(state->output_format) - 1);
        } else {
            fprintf(stderr,
                    "[engine] WARN: unsupported output format '%s'; using mkv\n",
                    item->valuestring);
        }
    }
    
    /* video_encoder */
    item = cJSON_GetObjectItem(root, "video_encoder");
    if (item && cJSON_IsString(item)) {
        const char *mapped = map_encoder_name(item->valuestring, true);
        strncpy(state->video_encoder, mapped, sizeof(state->video_encoder) - 1);
    }
    
    /* audio_encoder */
    item = cJSON_GetObjectItem(root, "audio_encoder");
    if (item && cJSON_IsString(item)) {
        const char *mapped = map_encoder_name(item->valuestring, false);
        strncpy(state->audio_encoder, mapped, sizeof(state->audio_encoder) - 1);
    }
    
    /* rate_control */
    item = cJSON_GetObjectItem(root, "rate_control");
    if (!item)
        item = cJSON_GetObjectItem(root, "video_rate_control");
    if (item && cJSON_IsString(item)) {
        if (!set_video_rate_control_from_string(state, item->valuestring)) {
            fprintf(stderr,
                    "[engine] WARN: invalid rate_control '%s'; using cqp\n",
                    item->valuestring);
        }
    }

    /* quality_cqp, with old video_quality fallback */
    bool has_quality_cqp = false;
    item = cJSON_GetObjectItem(root, "quality_cqp");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v >= 0 && v <= 51) {
            state->quality_cqp = v;
            has_quality_cqp = true;
        }
    }
    item = cJSON_GetObjectItem(root, "video_quality");
    if (!has_quality_cqp && item && cJSON_IsString(item)) {
        state->quality_cqp = legacy_quality_to_cqp(
            item->valuestring, state->quality_cqp);
    }

    /* video_bitrate */
    item = cJSON_GetObjectItem(root, "video_bitrate");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v >= 50 && v <= 300000)
            state->video_bitrate = v;
    }

    /* video_max_bitrate */
    item = cJSON_GetObjectItem(root, "video_max_bitrate");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v >= 50 && v <= 300000)
            state->video_max_bitrate = v;
    }
    if (state->video_max_bitrate < state->video_bitrate)
        state->video_max_bitrate = state->video_bitrate;
    
    /* audio_bitrate */
    item = cJSON_GetObjectItem(root, "audio_bitrate");
    if (item && cJSON_IsNumber(item)) {
        int v = item->valueint;
        if (v > 0 && v <= 512)  /* Reasonable range */
            state->audio_bitrate = v;
    }
    
    /* vaapi_device */
    item = cJSON_GetObjectItem(root, "vaapi_device");
    if (item && cJSON_IsString(item)) {
        strncpy(state->vaapi_device, item->valuestring,
                sizeof(state->vaapi_device) - 1);
    }
    
    /* output_dir or output_folder */
    item = cJSON_GetObjectItem(root, "output_dir");
    if (!item)
        item = cJSON_GetObjectItem(root, "output_folder");
    if (item && cJSON_IsString(item)) {
        char expanded[PATH_MAX];
        if (expand_path(item->valuestring, expanded, sizeof(expanded)) == 0) {
            strncpy(state->output_dir, expanded, sizeof(state->output_dir) - 1);
        } else {
            strncpy(state->output_dir, item->valuestring,
                    sizeof(state->output_dir) - 1);
        }
    }

    item = cJSON_GetObjectItem(root, "audio");
    parse_audio_config(state, item);
    
    cJSON_Delete(root);
    return CONFIG_LOAD_LOADED;
}

/* ── Argument parsing ────────────────────────────────────────────────────── */
static void parse_args(int argc, char *argv[], engine_state_t *state)
{
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--socket") == 0 && i + 1 < argc) {
            strncpy(state->socket_path, argv[++i],
                    sizeof(state->socket_path) - 1);
        } else if (strcmp(argv[i], "--output-dir") == 0 && i + 1 < argc) {
            strncpy(state->output_dir, argv[++i],
                    sizeof(state->output_dir) - 1);
        } else if (strcmp(argv[i], "--max-time") == 0 && i + 1 < argc) {
            int v = atoi(argv[++i]);
            state->max_time_sec = (v > 0) ? v : 60;
        } else if (strcmp(argv[i], "--capture-mode") == 0 && i + 1 < argc) {
            const char *mode = argv[++i];
            if (!set_capture_mode_from_string(state, mode)) {
                fprintf(stderr,
                        "[engine] WARN: invalid --capture-mode '%s'; using %s\n",
                        mode, capture_mode_to_string(state->capture_mode));
            }
        } else if (strcmp(argv[i], "--verbose") == 0) {
            g_verbose = 1;
        } else {
            fprintf(stderr, "[engine] unknown argument: %s\n", argv[i]);
        }
    }
}

/* ── main ────────────────────────────────────────────────────────────────── */
int main(int argc, char *argv[])
{
    engine_state_t state;
    memset(&state, 0, sizeof(state));
    state.server_fd     = -1;
    state.buffer_active = false;
    for (int i = 0; i < MAX_CLIENTS; i++)
        state.clients[i].fd = -1;

    /* Apply defaults first */
    apply_config_defaults(&state);
    
    /* Load config file (overrides defaults) */
    config_load_result_t config_load_result = load_config_file(&state);
    
    /* Socket path default */
    get_default_socket_path(state.socket_path, sizeof(state.socket_path));

    /* Override with command-line args (overrides config) */
    parse_args(argc, argv, &state);

    const char *config_summary =
        config_load_result == CONFIG_LOAD_LOADED ? "loaded existing local config" :
        config_load_result == CONFIG_LOAD_DEFAULTS_MISSING ?
            "no local config; using built-in defaults" :
            "local config unavailable; using built-in defaults";
    fprintf(stderr,
            "[engine] Settings: %s; %dx%d @ %d fps; video encoder: %s; "
            "format: %s; replay: %d s / %d MiB\n",
            config_summary, state.output_width, state.output_height, state.fps,
            state.video_encoder, state.output_format, state.max_time_sec,
            state.max_size_mb);

    /* Mutex initialisation */
    if (pthread_mutex_init(&state.clients_mutex, NULL) != 0) {
        fprintf(stderr, "[engine] FAIL: pthread_mutex_init\n");
        return 1;
    }

    /* Create clip output directory */
    if (mkdirs(state.output_dir) < 0)
        fprintf(stderr, "[engine] WARN: could not create output dir '%s': %s\n",
                state.output_dir, strerror(errno));

    /* Initialise libobs */
    if (!libobs_init(&state)) {
        pthread_mutex_destroy(&state.clients_mutex);
        return 1;
    }

    /* Create Unix domain socket server */
    state.server_fd = ipc_server_create(state.socket_path);
    if (state.server_fd < 0) {
        libobs_shutdown(&state);
        pthread_mutex_destroy(&state.clients_mutex);
        return 1;
    }

    /* Signal handlers — just set a flag; cleanup happens in main */
    signal(SIGTERM, handle_signal);
    signal(SIGINT,  handle_signal);
    signal(SIGPIPE, SIG_IGN);  /* avoid crash on broken client socket */

    /* Announce readiness — the Python UI waits for this line on stdout */
    printf("READY %s\n", state.socket_path);
    fflush(stdout);

    /* ── Event loop ────────────────────────────────────────────────────── */
    event_loop(&state);

    /* ── Shutdown sequence ─────────────────────────────────────────────── */

    /* Broadcast idle if the buffer was still active */
    if (state.buffer_active) {
        cJSON *ev = cJSON_CreateObject();
        if (ev) {
            cJSON_AddStringToObject(ev, "event", "status_changed");
            cJSON_AddStringToObject(ev, "status", "idle");
            broadcast_event(&state, ev);
            cJSON_Delete(ev);
        }
    }

    /* Close all client connections */
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (state.clients[i].active)
            close_client(&state, i);
    }

    /* Close and unlink the server socket */
    close(state.server_fd);
    unlink(state.socket_path);

    /* Shut down libobs (also stops replay buffer if still active) */
    libobs_shutdown(&state);

    pthread_mutex_destroy(&state.clients_mutex);

    /* Return appropriate exit code for restart handling by launcher */
    if (g_restart_requested) {
        return 42;  /* Signal launcher to restart engine */
    }
    return 0;  /* Normal shutdown */
}
