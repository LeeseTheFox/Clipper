# Test purpose and maintenance

Run the complete suite, type checks and lint from the repository root:

```sh
./tools/run_pytest_quiet.sh
```

## Retention rule

Every test should identify a plausible regression and observe its consequence
in Clipper. The test name should explain the scenario and expected behavior.
Add a short explanation when the reason for an internal invariant is not obvious.

- Keep protection against lost data, wrong capture or export output, broken
  platform integration, invalid input, resource leaks and lifecycle races.
- Exercise production code. A calculation copied into the test does not verify
  the production calculation. Source substrings do not prove execution.
- Prefer the existing behavioral test when it already detects the same failure.
  Fold persistence checks into a representative round trip; keep distinct
  normalization, migration and failure cases separate.
- Keep parameter cases for distinct boundaries, protocols or platform variants.
  Do not multiply ordinary values that exercise the same path just to add cases.
- Mock external dependencies when needed, but assert the resulting behavior.
  Callback order is worth testing when it prevents lost work or stale state.
- Avoid freezing wording, widget hierarchy, margins, colors, dependency versions
  or old implementation choices. Validate CSS syntax with GTK; assess appearance
  in the actual application. Test translated values independently of protocol IDs.
- Static checks are appropriate for deliberate repository-wide requirements,
  such as gettext usage, symbolic icon policy, bounded Flatpak permissions and
  pinned downloads. They should express the rule, not duplicate implementation.
- Native harnesses check C behavior, but cannot establish rendered pixels or
  hardware synchronization. Use the Flatpak build, relevant smoke tools, and
  visual checks when those outcomes matter.

A small test can protect an important boundary. A large test can add no useful
protection. Test count is an inventory, not a quality target.

## Coverage map

Each row describes the contract covered by a module. Individual test names
identify the specific scenario. Similar cases in native and Python
process matching intentionally cover separate production implementations.

| Module (`test_` prefix, `.py` suffix) | Long-term regression protection |
| --- | --- |
| audio_source_discovery | Identify real playback applications and microphones without system-client noise. |
| audio_source_watcher | Refresh discovery for relevant audio events only. |
| audio_tracks_view | Preserve routing and offline selections, filter non-descriptive media names, synchronize whitelist routes when games change, bound synchronous executable inference, tolerate unavailable game libraries, update gain and mute live, restore track gain after unmuting, keep slider travel fixed across percentage digit changes, discover sources on page activation and live events without starting capture, release inactive discovery work. |
| autostart | Honor portal approval and native desktop-file ownership; report registration failures. |
| capability_response | Share startup and window discovery, bound connection/response retries, release engine ownership, and reject late replies after reconnect, timeout or shutdown. |
| capture_modes | Migrate legacy capture labels and persist stable mode IDs. |
| clips_view | Rename and delete safely, fall back from failed Flatpak trash requests to host trash only for the same regular file, preserve clips and edits after failures or missing/timed-out helpers with copyable diagnostics, preserve the list model on deletion and show empty/search states, retain drafts and game metadata, bound asynchronous gallery/cache work, activate previews for in-window playback. Actual host trash metadata and recoverability require an installed smoke check. |
| clip_player_overlay | Render preview clipping and handoffs correctly, wait for a decoded frame before revealing or playing, cancel stale loads and animations, preserve playback across fullscreen layout changes, restore gallery focus and release media when the host is hidden. |
| config | Persist typed settings, recover malformed files, migrate schemas and preserve engine-owned tokens. |
| copy_pacing | Execute capture pacing, lifecycle and wire-format checks in the C implementation. |
| dialogs | Expose the appropriate capture instructions and copy the actual launch option. |
| display_auth | Recover valid X11 authorization after session credentials change. |
| editor_drafts | Save, restore, rename and reject stale or malformed edits without losing usable history. |
| editor_export | Preserve aspect ratio, timing, audio and codec contracts, including native encoder quality scales in exports and capability probes, VAAPI HEVC encoder headers and cropping in hvc1 containers and AV1/VP9 quality options scoped away from audio; validate output despite AAC priming packet order, report original encoding failures before shutdown noise and clean failed exports. GPU output correctness requires a real hardware export and decoded-frame inspection. |
| editor_export_dialog | Keep selected options, require overwrite confirmation, handle progress/cancel and block unsafe close. |
| editor_history | Maintain undo/redo branches, saved state and bounded/coalesced history. |
| editor_media | Preserve valid sample aspect ratio and reject invalid probe metadata. |
| editor_model | Maintain segment boundaries, time mappings and independent audio settings; preserve a final segment. |
| editor_preview | Keep cuts, seek/playback, live gain, decoder lifetime and stale callbacks consistent; release partial audio outputs after construction failures. |
| editor_timeline | Maintain click/playhead/zoom mappings, scroll limits and bounded waveform sampling. |
| editor_waveform | Preserve channel peaks and chunk boundaries; compare actual FFmpeg output across audio formats. |
| editor_window_shortcuts | Dispatch editor actions correctly, including physical keys under non-Latin layouts. |
| editor_window_timeline_interactions | Keep selection, scrubbing, gain editing, zoom anchoring and undo history consistent. |
| editor_window_transport | Coordinate seeking, volume, fullscreen and close lifecycle with the preview. |
| editor_window_wipe | Clear saved and in-memory edits only after confirmation; preserve them on cancellation. |
| engine_client | Encode IPC requests and complete saves once despite response/event ordering. |
| engine_ipc | Exercise the real engine socket, capabilities, recording lifecycle, pushed state snapshots, saved files and concurrent clients. |
| engine_manager | Launch with the correct environment, deliver readiness/exits without polling, reject retired process callbacks and distinguish restart from normal exit. |
| flatpak_metadata | Preserve desktop identity, icon policy, portable host helpers, pinned sources and narrow permissions. |
| focus_helpers | Preserve stable dropdown IDs, handle invalid selection and prevent controls stealing input. |
| game_audio_learning | Learn audio identity only from verified game processes and update the matching configuration. |
| game_capture | Publish/repair payloads atomically, preserve working copies, quote executable launch arguments and preserve overlay precedence in the generated preload wrapper to prevent launch hangs. Real overlay loading and captured frames require a hardware smoke check. |
| game_capture_frame_smoke | Consume command replies despite interleaved state events, wait for actual save completion and fail promptly on engine errors/disconnects. |
| game_details | Replace executable identity safely, preserve user settings and resolve the correct Steam account. |
| game_icons | Find appropriate game artwork, avoid blocking discovery and bound extracted-icon caches. |
| hotkeys | Normalize valid chords, use physical keys and handle portal approval/cancellation races. |
| i18n | Discover catalogs, resolve locales, load the saved language early and validate translated UI coverage. |
| launchers | Reject shell syntax errors using the shell parser; exercise Makefile build/install rules so OBS muxing and NVENC probe helpers remain executable beside the engine; restore clangd headers from cached Flatpak modules when unpacked sources are absent. Actual GPU encoding requires a hardware smoke check. |
| lazy_video_buffers | Execute allocation/reuse/failure cleanup against the shipped C patch helpers. |
| local_flatpak_install | Install bundles without competing repository origins, preserve data across migration and retries, and leave running apps untouched when bundle preparation fails. Real Flatpak origin behavior requires an isolated integration check. |
| log_window | Stop subscriptions and scheduled refreshes after the window closes. |
| logs | Preserve useful diagnostics, bound history and carry logs through process handoff. |
| main | Coordinate startup/setup, tray lifecycle, recording, engine restarts, portal decisions, updates, capability ownership, event-driven state recovery and session shutdown; keep idle foreground/background launches free of recording engines, reuse live engines for discovery, distinguish capture startup from temporary probes and protect active replay through unknown monitor state and IPC reconnects. |
| main_window_preferences | Pass catalog language IDs through preferences and track both physical Shift keys. |
| monitor_host | Execute C host process detection, fixed Steam operations, scope isolation, live rule reloads and complete state recovery after reconnect. |
| monitor_ipc | Validate complete snapshots and incoming events, restrict socket access and interrupt idle connections on shutdown. |
| monitor_manager | Use the correct native or Flatpak helper, validate host results and own one monitor process. |
| native_capture | Reject unsupported direct-capture scenes and preserve idle-render consumers. |
| pipewire_import_cache | Retain/release imported buffers and synchronization handles correctly, with and without explicit sync. |
| process_watcher | Match and deduplicate game identities across native, Steam, Wine and sandbox environments. |
| recording_preview | Keep recording identity current and long names ellipsized within real GTK preview allocations for landscape, portrait and ultrawide frames. |
| release_notes | Generate accurate categorized history from actual Git repositories and semantic tags. |
| release_version | Reject malformed tags and mismatched application release versions. |
| settings_view | Apply settings correctly, respect platform approval, join background discovery, detach retired windows, reuse cached options and clamp encoder controls. |
| screenshots | Migrate theme-aware galleries to dark screenshots and refresh references repeatedly without losing translated labels or prose; reject missing gallery references before writing. Actual GTK rendering and byte-for-byte reproducibility are checked with `./tools/generate_screenshots.sh --check`. |
| setup_window | Persist setup choices within engine-supported clip limits, respect existing settings, share encoder discovery, reject responses after close, and complete or skip setup consistently. |
| shortcut_dialog | Validate accelerator syntax and open the correct shortcut collection from menus and physical keys under non-Latin layouts. |
| sound_feedback | Play the bundled asset with bounded gain and tolerate missing/unavailable media. |
| startup_benchmark | Measure the right processes and decode desktop presentation/handoff state accurately. |
| steam | Discover installations/accounts and change launch options without corrupting or claiming user-owned data. |
| suika_game_model | Keep drops in bounds, prevent perfectly balanced repeated-drop towers and enforce momentum-preserving fruit merges at resting and solver-created contacts, bounded size-scaled neighbor impulses, score, terminal-size rules, per-fruit overflow grace/reset timing and real-time fixed-step physics independent of frame timing. |
| text_helpers | Preserve readable/full labels and escape user-controlled markup. |
| theme_adaptation | Parse editor CSS with GTK and reject errors. |
| tray | Maintain the public D-Bus/icon contract and flush replies before activation or shutdown. |
| update_controller | Coordinate checks, notifications, cancellation and restarts without interrupting saves or edits. |
| update_publication | Publish only complete release assets and prevent overwriting public releases. |
| updates | Reject invalid/untrusted updates, verify downloads and installation commits, prevent downgrades. |
| vaapi_direct_surfaces | Execute queue ownership, teardown, fence-wait and validation-cache C contracts. |
| video_output | Connect the color pipeline and fall back safely when GL elements or links fail. |
| video_player | Preserve playback/seek state, detach media before background teardown, letterbox and honor physical shortcuts. |
| whitelist_view | Persist scoped identities and present/search/remove the intended game. |
| window_state | Restore usable sizes after monitor changes and preserve other windows' saved state. |
