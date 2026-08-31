/*
 * vkcapture_spike.c - Phase 1 spike: obs-vkcapture hook detect + replay buffer
 *
 * Goal: confirm that linux-vkcapture.so (the OBS-side plugin for obs-vkcapture)
 * can be loaded inside our own libobs process, that the source's "get_hooked"
 * proc correctly reports a game connecting/disconnecting, and that we can
 * start/stop the replay buffer in response.
 *
 * Build:  make -C engine/spike vkcapture_spike
 * Run:    engine/spike/run_vkcapture_spike.sh
 *         (then, in a second terminal: obs-gamecapture /path/to/your/game)
 *
 * Expected output when a game is launched with obs-gamecapture:
 *   [vkspike] Polling get_hooked... (loop)
 *   [vkspike] *** GAME HOOKED: game_executable_name ***
 *   [vkspike] --- replay buffer started ---
 *   [vkspike] *** GAME UNHOOKED ***   (when you close the game)
 *   [vkspike] --- replay buffer stopped ---
 */

#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <unistd.h>
#include <time.h>
#include <signal.h>
#include <sys/stat.h>

#include <obs/obs.h>
#include <obs/obs-data.h>
#include <obs/callback/calldata.h>
#include <obs/callback/proc.h>

/* ── Paths ──────────────────────────────────────────────────────────────── */

/* Main OBS Flatpak — same as the existing libobs_spike */
#define OBS_LIB_PATH \
    "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
#define OBS_PLUGIN_BIN_PATH  OBS_LIB_PATH "/obs-plugins"
#define CLIPPER_OBS_DATA \
    "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/share/obs"
#define OBS_PLUGIN_DATA_PATH \
    CLIPPER_OBS_DATA "/obs-plugins/%module%"

/* OBSVkCapture Flatpak extension — separate from the main OBS install */
#define VKC_EXT_BASE \
    "/var/lib/flatpak/runtime/com.obsproject.Studio.Plugin.OBSVkCapture" \
    "/x86_64/stable/active/files"
#define VKC_PLUGIN_SO   VKC_EXT_BASE "/lib/obs-plugins/linux-vkcapture.so"
#define VKC_PLUGIN_DATA VKC_EXT_BASE "/share/obs/obs-plugins/linux-vkcapture"

#define OUTPUT_DIR "/tmp/clipper_vkspike"

/* ── Globals ────────────────────────────────────────────────────────────── */

static volatile sig_atomic_t g_quit = 0;

static void handle_sig(int s)
{
    (void)s;
    g_quit = 1;
}

/* ── Minimal log handler — suppress noise, keep warnings/errors ─────────── */
static void spike_log(int log_level, const char *msg, va_list args, void *p)
{
    (void)p;
    /* Show INFO and above so we can see [linux-vkcapture] "Client connected" */
    if (log_level <= LOG_INFO) {
        const char *lvl = log_level <= LOG_ERROR   ? "ERROR"
                        : log_level <= LOG_WARNING ? "WARN "
                        :                           "info ";
        fprintf(stderr, "[obs/%s] ", lvl);
        vfprintf(stderr, msg, args);
        fputc('\n', stderr);
    }
}

/* ── Replay buffer helpers (copied from libobs_spike) ───────────────────── */

static obs_output_t  *g_replay  = NULL;
static obs_encoder_t *g_venc    = NULL;
static obs_encoder_t *g_aenc    = NULL;
static bool           g_rb_active = false;

static bool start_replay_buffer(void)
{
    if (g_rb_active) return true;
    if (!obs_output_start(g_replay)) {
        const char *err = obs_output_get_last_error(g_replay);
        fprintf(stderr, "[vkspike] WARN: obs_output_start failed: %s\n",
                err ? err : "(none)");
        return false;
    }
    g_rb_active = true;
    printf("[vkspike] --- replay buffer started ---\n");
    fflush(stdout);
    return true;
}

static void stop_replay_buffer(void)
{
    if (!g_rb_active) return;
    obs_output_stop(g_replay);
    g_rb_active = false;
    printf("[vkspike] --- replay buffer stopped ---\n");
    fflush(stdout);
}

static void save_replay_buffer(void)
{
    if (!g_rb_active) return;
    calldata_t cd = {0};
    proc_handler_t *ph = obs_output_get_proc_handler(g_replay);
    bool ok = proc_handler_call(ph, "save", &cd);
    calldata_free(&cd);
    if (ok) {
        /* Get saved path */
        calldata_t rcd = {0};
        if (proc_handler_call(ph, "get_last_replay", &rcd)) {
            const char *path = calldata_string(&rcd, "path");
            printf("[vkspike] *** CLIP SAVED: %s ***\n", path ? path : "(unknown path)");
            fflush(stdout);
        }
        calldata_free(&rcd);
    } else {
        printf("[vkspike] WARN: save proc failed\n");
    }
}

int main(void)
{
    signal(SIGINT,  handle_sig);
    signal(SIGTERM, handle_sig);

    printf("[vkspike] =============================================================\n");
    printf("[vkspike] obs-vkcapture hook detection spike\n");
    printf("[vkspike] =============================================================\n");
    printf("[vkspike]\n");
    printf("[vkspike] While this is running, launch your game in another terminal:\n");
    printf("[vkspike]   obs-gamecapture /path/to/your/game\n");
    printf("[vkspike] or: OBS_VKCAPTURE=1 /path/to/your/game\n");
    printf("[vkspike]\n");
    printf("[vkspike] Press Ctrl-C to exit cleanly.\n");
    printf("[vkspike] =============================================================\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 1. obs_startup
     * ------------------------------------------------------------------ */
    base_set_log_handler(spike_log, NULL);
    if (!obs_startup("en-US", NULL, NULL)) {
        fprintf(stderr, "[vkspike] FAIL: obs_startup\n");
        return 1;
    }
    printf("[vkspike] OK: obs_startup\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 1b. Data path
     * ------------------------------------------------------------------ */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    obs_add_data_path(CLIPPER_OBS_DATA "/libobs/");
#pragma GCC diagnostic pop

    /* ------------------------------------------------------------------
     * 2. Module search paths (main OBS plugins + vkcapture extension)
     * ------------------------------------------------------------------ */
    obs_add_module_path(OBS_PLUGIN_BIN_PATH, OBS_PLUGIN_DATA_PATH);
    printf("[vkspike] OK: main OBS module path added\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 3. Reset video (LIBGL_ALWAYS_SOFTWARE=1 set by run script)
     * ------------------------------------------------------------------ */
    struct obs_video_info ovi = {
        .graphics_module = "libobs-opengl",
        .fps_num         = 30,
        .fps_den         = 1,
        .base_width      = 1920,
        .base_height     = 1080,
        .output_width    = 1920,
        .output_height   = 1080,
        .output_format   = VIDEO_FORMAT_NV12,
        .adapter         = 0,
        .gpu_conversion  = true,
        .scale_type      = OBS_SCALE_BILINEAR,
    };
    int vr = obs_reset_video(&ovi);
    if (vr != OBS_VIDEO_SUCCESS) {
        fprintf(stderr, "[vkspike] FAIL: obs_reset_video -> %d\n", vr);
        obs_shutdown();
        return 1;
    }
    printf("[vkspike] OK: obs_reset_video\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 4. Reset audio
     * ------------------------------------------------------------------ */
    struct obs_audio_info oai = {
        .samples_per_sec = 44100,
        .speakers        = SPEAKERS_STEREO,
    };
    if (!obs_reset_audio(&oai)) {
        fprintf(stderr, "[vkspike] FAIL: obs_reset_audio\n");
        obs_shutdown();
        return 1;
    }
    printf("[vkspike] OK: obs_reset_audio\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 5. Load modules selectively
     *    Note: linux-vkcapture.so is loaded directly by full path because
     *    it lives in a different Flatpak extension, not in the main
     *    OBS plugin directory that obs_add_module_path points to.
     * ------------------------------------------------------------------ */
    printf("[vkspike] Loading modules...\n");
    fflush(stdout);

    /* Standard OBS modules (replay buffer needs obs-ffmpeg + obs-x264 + obs-outputs) */
    const char *std_mods[] = {
        OBS_PLUGIN_BIN_PATH "/obs-ffmpeg.so",
        OBS_PLUGIN_BIN_PATH "/obs-x264.so",
        OBS_PLUGIN_BIN_PATH "/obs-outputs.so",
        OBS_PLUGIN_BIN_PATH "/linux-pipewire.so",
    };
    for (int i = 0; i < (int)(sizeof(std_mods)/sizeof(std_mods[0])); i++) {
        obs_module_t *mod = NULL;
        int r = obs_open_module(&mod, std_mods[i], NULL);
        if (r == MODULE_SUCCESS) {
            obs_init_module(mod);
            printf("[vkspike] OK: loaded %s\n", std_mods[i]);
        } else {
            printf("[vkspike] WARN: %s -> %d (skipping)\n", std_mods[i], r);
        }
    }

    /* linux-vkcapture.so — loaded by full path, data dir passed explicitly */
    {
        obs_module_t *vkc_mod = NULL;
        int r = obs_open_module(&vkc_mod, VKC_PLUGIN_SO, VKC_PLUGIN_DATA);
        if (r != MODULE_SUCCESS) {
            fprintf(stderr,
                    "[vkspike] FAIL: obs_open_module(linux-vkcapture) -> %d\n"
                    "[vkspike]   Expected path: %s\n"
                    "[vkspike]   Run `flatpak list | grep OBSVkCapture` to verify install.\n",
                    r, VKC_PLUGIN_SO);
            obs_shutdown();
            return 1;
        }
        bool ok = obs_init_module(vkc_mod);
        if (!ok) {
            fprintf(stderr, "[vkspike] WARN: obs_init_module(linux-vkcapture) returned false\n");
        } else {
            printf("[vkspike] OK: linux-vkcapture loaded\n");
        }
    }

    obs_post_load_modules();
    printf("[vkspike] OK: all modules loaded\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 6. Verify the vkcapture-source type was registered
     * ------------------------------------------------------------------ */
    if (!obs_source_get_display_name("vkcapture-source")) {
        fprintf(stderr,
            "[vkspike] FAIL: 'vkcapture-source' type not registered.\n"
            "[vkspike]   The linux-vkcapture module loaded but did not register its source.\n"
            "[vkspike]   This may be a version incompatibility or a missing dependency.\n");
        obs_shutdown();
        return 1;
    }
    printf("[vkspike] OK: 'vkcapture-source' type registered\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 7. Create the vkcapture-source (the OBS-side server)
     *    This opens the abstract Unix socket \0/com/obsproject/vkcapture
     *    that the game hook (obs-gamecapture) connects to.
     * ------------------------------------------------------------------ */
    obs_data_t *src_settings = obs_data_create();
    /* show_cursor and allow_transparency are the known settings keys */
    obs_data_set_bool(src_settings, "show_cursor", false);
    obs_source_t *vkc_source = obs_source_create(
        "vkcapture-source", "clipper_game_capture", src_settings, NULL);
    obs_data_release(src_settings);

    if (!vkc_source) {
        fprintf(stderr, "[vkspike] FAIL: obs_source_create(vkcapture-source) returned NULL\n");
        obs_shutdown();
        return 1;
    }

    /*
     * Put the source on channel 0 (the main compositing channel).
     * This makes it "active" so its video_tick callback runs every frame.
     * Without this the source never claims connecting clients and they
     * disconnect immediately after the initial handshake.
     */
    obs_set_output_source(0, vkc_source);

    printf("[vkspike] OK: vkcapture-source created and active on channel 0\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 8. Create replay buffer output + encoders
     * ------------------------------------------------------------------ */
    mkdir(OUTPUT_DIR, 0755);

    obs_data_t *rb_settings = obs_data_create();
    obs_data_set_string(rb_settings, "directory", OUTPUT_DIR);
    obs_data_set_string(rb_settings, "format",    "%CCYY-%MM-%DD_%hh-%mm-%ss");
    obs_data_set_string(rb_settings, "extension", "mkv");
    obs_data_set_int(rb_settings,    "max_time_sec", 30);
    obs_data_set_int(rb_settings,    "max_size_mb",  0);

    g_replay = obs_output_create("replay_buffer", "clipper_replay", rb_settings, NULL);
    obs_data_release(rb_settings);
    if (!g_replay) {
        fprintf(stderr, "[vkspike] FAIL: obs_output_create(replay_buffer)\n");
        obs_source_release(vkc_source);
        obs_shutdown();
        return 1;
    }

    g_venc = obs_video_encoder_create("obs_x264", "clipper_venc", NULL, NULL);
    if (!g_venc) {
        fprintf(stderr, "[vkspike] FAIL: obs_x264 encoder\n");
        obs_output_release(g_replay);
        obs_source_release(vkc_source);
        obs_shutdown();
        return 1;
    }
    obs_encoder_set_video(g_venc, obs_get_video());

    g_aenc = obs_audio_encoder_create("ffmpeg_aac", "clipper_aenc", NULL, 0, NULL);
    if (!g_aenc) {
        fprintf(stderr, "[vkspike] FAIL: ffmpeg_aac encoder\n");
        obs_encoder_release(g_venc);
        obs_output_release(g_replay);
        obs_source_release(vkc_source);
        obs_shutdown();
        return 1;
    }
    obs_encoder_set_audio(g_aenc, obs_get_audio());

    obs_output_set_video_encoder(g_replay, g_venc);
    obs_output_set_audio_encoder(g_replay, g_aenc, 0);

    printf("[vkspike] OK: replay buffer and encoders ready\n\n");
    printf("[vkspike] Now waiting for a game to connect...\n");
    printf("[vkspike] (Launch your game with: obs-gamecapture /path/to/game)\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 9. Main poll loop: check get_hooked every 500ms
     *    Track state transitions and start/stop replay buffer accordingly.
     * ------------------------------------------------------------------ */
    bool was_hooked = false;
    int  hooked_for = 0;      /* seconds hooked (for auto-save demo) */
    bool clip_saved = false;  /* save once per hook session */

    proc_handler_t *src_ph = obs_source_get_proc_handler(vkc_source);

    while (!g_quit) {
        { struct timespec ts = {0, 500000000L}; nanosleep(&ts, NULL); } /* 500ms */

        if (!src_ph) {
            src_ph = obs_source_get_proc_handler(vkc_source);
            if (!src_ph) continue;
        }

        /* Call get_hooked proc */
        calldata_t cd = {0};
        bool call_ok = proc_handler_call(src_ph, "get_hooked", &cd);

        bool hooked = false;
        const char *exe = NULL;

        if (call_ok) {
            hooked = calldata_bool(&cd, "hooked");
            exe    = calldata_string(&cd, "executable");
        }
        calldata_free(&cd);

        if (hooked && !was_hooked) {
            /* Transition: unhooked → hooked */
            printf("[vkspike] *** GAME HOOKED: %s ***\n", exe ? exe : "(unknown)");
            fflush(stdout);
            was_hooked  = true;
            hooked_for  = 0;
            clip_saved  = false;
            start_replay_buffer();

        } else if (!hooked && was_hooked) {
            /* Transition: hooked → unhooked */
            printf("[vkspike] *** GAME UNHOOKED ***\n");
            fflush(stdout);
            /* Save one last clip before stopping */
            if (!clip_saved && g_rb_active) {
                printf("[vkspike] Saving final clip before stop...\n");
                fflush(stdout);
                save_replay_buffer();
                sleep(2); /* let the muxer finish */
            }
            stop_replay_buffer();
            was_hooked = false;
            hooked_for = 0;
            clip_saved = false;

        } else if (hooked) {
            hooked_for++;  /* each tick = 0.5s; hooked_for == 20 → ~10s */

            /* Auto-save a clip after 10 seconds to prove the capture works */
            if (hooked_for == 20 && !clip_saved) {
                printf("[vkspike] 10 seconds hooked — saving demo clip...\n");
                fflush(stdout);
                save_replay_buffer();
                clip_saved = true;
            }
        }
    }

    /* ------------------------------------------------------------------
     * 10. Clean shutdown
     * ------------------------------------------------------------------ */
    printf("\n[vkspike] Shutting down...\n");
    fflush(stdout);

    if (g_rb_active && !clip_saved) {
        save_replay_buffer();
        sleep(2);
    }
    stop_replay_buffer();

    obs_encoder_release(g_aenc);
    obs_encoder_release(g_venc);
    obs_output_release(g_replay);
    obs_set_output_source(0, NULL); /* detach before releasing */
    obs_source_release(vkc_source);
    obs_shutdown();

    printf("[vkspike] Done. Clips (if any) are in %s\n", OUTPUT_DIR);
    printf("[vkspike] =============================================================\n");
    return 0;
}
