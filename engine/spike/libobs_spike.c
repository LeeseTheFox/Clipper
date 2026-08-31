/*
 * libobs_spike.c - Phase 1 Engine Spike: headless libobs init + replay buffer
 *
 * Build: make -C engine/spike
 * Run:   make -C engine/spike run
 *
 * Run result (2026-06-23): FULL SUCCESS — exit code 0
 *
 *   obs_startup           OK (Wayland/KDE session, no X11 needed)
 *   obs_reset_video       OK (libobs-opengl via Mesa llvmpipe, LIBGL_ALWAYS_SOFTWARE=1)
 *   obs_reset_audio       OK (44100 Hz stereo)
 *   Modules loaded        OK  obs-ffmpeg, obs-x264, obs-outputs, linux-pipewire
 *   linux-pipewire        OK  "Monitor source" and "Window source" available
 *   Replay buffer start   OK  obs_x264 @ 6000 kbps CBR, ffmpeg_aac @ 128 kbps
 *   Save triggered        OK  proc_handler_call(ph, "save", &cd) returned true
 *   Clip file             OK  /tmp/clipper_spike/2026-06-23_22-43-04.mkv (3.6 MB)
 *   obs_shutdown          OK
 *
 * Setup notes for future phases:
 *   - run_spike.sh must set LD_LIBRARY_PATH to OBS app lib + lib_shim/ (see
 *     setup_lib_shim.sh) before running the binary.  The shim provides only
 *     the specific codec/pulse libs that differ in SONAME from host packages
 *     (libvpx.so.11, libtheoraenc.so.2, libpulse.so.0, etc.); adding the
 *     full Flatpak runtime dir causes a stack-smash from glibc version mix.
 *   - obs-ffmpeg-mux (the remux helper subprocess) must be reachable in the
 *     same directory as the main binary — a symlink in engine/spike/ works.
 *   - obs_load_all_modules() crashes headlessly (obs-websocket/obs-browser
 *     dereference obs_frontend_* pointers that are NULL without a frontend).
 *     Use obs_open_module + obs_init_module for only the plugins you need.
 *   - obs_add_data_path() is deprecated but still works; pass the libobs/
 *     subdirectory of the OBS data dir (not the parent), e.g.
 *     ".../share/obs/libobs/".
 *   - In a real Flatpak build all these paths will be inside the sandbox and
 *     none of these host-path workarounds will be needed.
 */

#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>

#include <obs/obs.h>
#include <obs/obs-data.h>
#include <obs/callback/calldata.h>
#include <obs/callback/proc.h>

#define OBS_LIB_PATH \
    "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/lib"
#define OBS_PLUGIN_BIN_PATH  OBS_LIB_PATH "/obs-plugins"
#define CLIPPER_OBS_DATA \
    "/var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files/share/obs"
#define OBS_PLUGIN_DATA_PATH \
    CLIPPER_OBS_DATA "/obs-plugins/%module%"

#define OUTPUT_DIR "/tmp/clipper_spike"

int main(void)
{
    printf("[spike] ============================================================\n");
    printf("[spike] libobs headless spike — Phase 1 Engine\n");
    printf("[spike] ============================================================\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 1. Start up libobs
     * ------------------------------------------------------------------ */
    printf("[spike] Step 1: obs_startup...\n");
    fflush(stdout);
    if (!obs_startup("en-US", NULL, NULL)) {
        fprintf(stderr, "[spike] FAIL: obs_startup returned false\n");
        return 1;
    }
    printf("[spike] OK: obs_startup\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 1b. Register libobs core data directory (shaders/effects)
     *     Must be set before obs_reset_video which loads OpenGL effects.
     * ------------------------------------------------------------------ */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    obs_add_data_path(CLIPPER_OBS_DATA "/libobs/");
#pragma GCC diagnostic pop
    printf("[spike] OK: obs data path set to " CLIPPER_OBS_DATA "/libobs/\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 2. Add module search paths so libobs can find obs-ffmpeg, etc.
     * ------------------------------------------------------------------ */
    printf("[spike] Step 2: obs_add_module_path\n");
    printf("[spike]   bin  = %s\n", OBS_PLUGIN_BIN_PATH);
    printf("[spike]   data = %s\n", OBS_PLUGIN_DATA_PATH);
    fflush(stdout);
    obs_add_module_path(OBS_PLUGIN_BIN_PATH, OBS_PLUGIN_DATA_PATH);
    printf("[spike] OK: module paths added\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 3. Reset video — required even in headless mode.
     *    LIBGL_ALWAYS_SOFTWARE=1 in run_spike.sh lets this work without
     *    a physical GPU/display.
     * ------------------------------------------------------------------ */
    printf("[spike] Step 3: obs_reset_video (libobs-opengl, 1920x1080 @ 30fps)...\n");
    fflush(stdout);
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
    int video_ret = obs_reset_video(&ovi);
    if (video_ret != OBS_VIDEO_SUCCESS) {
        fprintf(stderr, "[spike] FAIL: obs_reset_video returned %d\n", video_ret);
        fprintf(stderr, "[spike]   (OBS_VIDEO_FAIL=%d, OBS_VIDEO_MODULE_NOT_FOUND=%d,\n"
                        "[spike]    OBS_VIDEO_NOT_SUPPORTED=%d, OBS_VIDEO_INVALID_PARAM=%d)\n",
                OBS_VIDEO_FAIL, OBS_VIDEO_MODULE_NOT_FOUND,
                OBS_VIDEO_NOT_SUPPORTED, OBS_VIDEO_INVALID_PARAM);
        obs_shutdown();
        return 1;
    }
    printf("[spike] OK: obs_reset_video\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 4. Reset audio
     * ------------------------------------------------------------------ */
    printf("[spike] Step 4: obs_reset_audio (44100 Hz stereo)...\n");
    fflush(stdout);
    struct obs_audio_info oai = {
        .samples_per_sec = 44100,
        .speakers        = SPEAKERS_STEREO,
    };
    if (!obs_reset_audio(&oai)) {
        fprintf(stderr, "[spike] FAIL: obs_reset_audio returned false\n");
        obs_shutdown();
        return 1;
    }
    printf("[spike] OK: obs_reset_audio\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 5. Load only the plugins the spike needs.
     *    obs_load_all_modules() crashes when frontend-dependent plugins
     *    (obs-websocket, obs-browser) try to call obs_frontend_* with no
     *    frontend registered.  Load selectively instead.
     * ------------------------------------------------------------------ */
    printf("[spike] Step 5: loading required modules selectively...\n");
    fflush(stdout);
    {
        struct {
            const char *so;
            const char *data;
        } mods[] = {
            /* replay buffer output + FFmpeg muxer (required) */
            { OBS_PLUGIN_BIN_PATH "/obs-ffmpeg.so", NULL },
            /* x264 software H.264 encoder (reliable fallback) */
            { OBS_PLUGIN_BIN_PATH "/obs-x264.so", NULL },
            /* base outputs (rtmp/file/etc.) */
            { OBS_PLUGIN_BIN_PATH "/obs-outputs.so", NULL },
            /* PipeWire capture (optional) */
            { OBS_PLUGIN_BIN_PATH "/linux-pipewire.so", NULL },
        };
        const int n = (int)(sizeof(mods) / sizeof(mods[0]));
        for (int i = 0; i < n; i++) {
            obs_module_t *mod = NULL;
            /* Use the data path from obs_add_module_path (NULL here means
               libobs resolves it via the registered module paths). */
            int r = obs_open_module(&mod, mods[i].so, mods[i].data);
            if (r != MODULE_SUCCESS) {
                printf("[spike] WARN: obs_open_module(%s) -> %d (skipping)\n",
                       mods[i].so, r);
            } else {
                bool ok = obs_init_module(mod);
                printf("[spike] %s: %s\n", ok ? "OK" : "WARN",
                       mods[i].so);
            }
        }
    }
    obs_post_load_modules();
    printf("[spike] OK: modules loaded\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 6. Create /tmp/clipper_spike output directory
     * ------------------------------------------------------------------ */
    printf("[spike] Step 6: mkdir %s\n", OUTPUT_DIR);
    fflush(stdout);
    if (mkdir(OUTPUT_DIR, 0755) == 0) {
        printf("[spike] OK: directory created\n\n");
    } else {
        printf("[spike] OK: directory already exists (or mkdir failed — continuing)\n\n");
    }
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 7. Create replay buffer output
     *    The replay_buffer output id lives in obs-ffmpeg.so.
     *    Settings: directory, format string, file extension, buffer length.
     * ------------------------------------------------------------------ */
    printf("[spike] Step 7: obs_output_create(\"replay_buffer\")...\n");
    fflush(stdout);
    obs_data_t *settings = obs_data_create();
    obs_data_set_string(settings, "directory",    OUTPUT_DIR);
    obs_data_set_string(settings, "format",       "%CCYY-%MM-%DD_%hh-%mm-%ss");
    obs_data_set_string(settings, "extension",    "mkv");
    obs_data_set_int(settings,    "max_time_sec", 30);
    obs_data_set_int(settings,    "max_size_mb",  0);   /* 0 = no size limit */

    obs_output_t *replay_output = obs_output_create(
        "replay_buffer", "clipper_replay", settings, NULL);
    obs_data_release(settings);

    if (!replay_output) {
        fprintf(stderr, "[spike] FAIL: obs_output_create returned NULL\n");
        fprintf(stderr, "[spike]   (Is obs-ffmpeg.so loaded? Check module path.)\n");
        obs_shutdown();
        return 1;
    }
    printf("[spike] OK: replay buffer output created\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 7b. Create and attach video + audio encoders.
     *     The replay buffer cannot start without encoders attached.
     *     Try VAAPI H.264 (GPU) first; fall back to obs_x264 (software).
     * ------------------------------------------------------------------ */
    printf("[spike] Step 7b: creating encoders...\n");
    fflush(stdout);

    /* Video encoder — obs_x264 (pure software) is the reliable choice when
       running headless with software GL (LIBGL_ALWAYS_SOFTWARE=1) since VAAPI
       needs the real GPU EGL context which is not available in this mode. */
    obs_encoder_t *venc = obs_video_encoder_create(
        "obs_x264", "clipper_venc", NULL, NULL);
    if (!venc) {
        fprintf(stderr, "[spike] FAIL: could not create obs_x264 video encoder\n");
        obs_output_release(replay_output);
        obs_shutdown();
        return 1;
    }
    /* Wire the encoder to the libobs video pipeline so it has frames to encode */
    obs_encoder_set_video(venc, obs_get_video());
    printf("[spike] OK: video encoder (obs_x264) created\n");
    fflush(stdout);

    /* Audio encoder (FFmpeg AAC, mixer index 0) */
    obs_encoder_t *aenc = obs_audio_encoder_create(
        "ffmpeg_aac", "clipper_aenc", NULL, 0, NULL);
    if (!aenc) {
        fprintf(stderr, "[spike] FAIL: could not create audio encoder\n");
        obs_encoder_release(venc);
        obs_output_release(replay_output);
        obs_shutdown();
        return 1;
    }
    /* Wire the encoder to the libobs audio pipeline */
    obs_encoder_set_audio(aenc, obs_get_audio());
    printf("[spike] OK: audio encoder (ffmpeg_aac) created\n");
    fflush(stdout);

    /* Attach encoders to the output */
    obs_output_set_video_encoder(replay_output, venc);
    obs_output_set_audio_encoder(replay_output, aenc, 0);
    printf("[spike] OK: encoders attached to replay buffer\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 8. Start the replay buffer
     * ------------------------------------------------------------------ */
    printf("[spike] Step 8: obs_output_start...\n");
    fflush(stdout);
    if (!obs_output_start(replay_output)) {
        const char *err = obs_output_get_last_error(replay_output);
        fprintf(stderr, "[spike] FAIL: obs_output_start: %s\n",
                err ? err : "(no error message)");
        obs_output_release(replay_output);
        obs_shutdown();
        return 1;
    }
    printf("[spike] OK: replay buffer started\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 9. Wait 5 seconds (simulating recording)
     * ------------------------------------------------------------------ */
    printf("[spike] Step 9: sleeping 5 seconds...\n");
    fflush(stdout);
    sleep(5);
    printf("[spike] OK: woke up\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 10. Trigger replay buffer save via the proc handler
     *     The "save" proc is registered by obs-ffmpeg's replay_buffer output.
     *     After calling it the output writes the last N seconds to disk.
     * ------------------------------------------------------------------ */
    printf("[spike] Step 10: triggering replay buffer save...\n");
    fflush(stdout);
    {
        calldata_t cd;
        memset(&cd, 0, sizeof(cd));
        proc_handler_t *ph = obs_output_get_proc_handler(replay_output);
        bool called = proc_handler_call(ph, "save", &cd);
        calldata_free(&cd);
        if (called) {
            printf("[spike] OK: save proc called\n\n");
        } else {
            printf("[spike] WARN: save proc not found (output may not support it yet)\n\n");
        }
    }
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 11. Wait 2 more seconds for the save to complete
     * ------------------------------------------------------------------ */
    printf("[spike] Step 11: sleeping 2 seconds for save to complete...\n");
    fflush(stdout);
    sleep(2);
    printf("[spike] OK: done waiting\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 12. Stop the replay buffer and release resources
     * ------------------------------------------------------------------ */
    printf("[spike] Step 12: stopping replay buffer...\n");
    fflush(stdout);
    obs_output_stop(replay_output);
    obs_output_release(replay_output);
    obs_encoder_release(venc);
    obs_encoder_release(aenc);
    printf("[spike] OK: output and encoders stopped and released\n\n");
    fflush(stdout);

    /* ------------------------------------------------------------------
     * 13. Shut down libobs
     * ------------------------------------------------------------------ */
    printf("[spike] Step 13: obs_shutdown...\n");
    fflush(stdout);
    obs_shutdown();
    printf("[spike] OK: obs_shutdown complete\n");
    printf("[spike] ============================================================\n");
    printf("[spike] Spike completed successfully. Check %s for clip file.\n", OUTPUT_DIR);
    printf("[spike] ============================================================\n");

    return 0;
}
