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

A small test can protect an important boundary. A large test can add no useful
protection. Test count is an inventory, not a quality target.

## September 2026 audit

Reviewed the scenarios, assertions and parameter cases in all 71 pytest modules,
including their compiled native harnesses. The suite went from 1,227 to 1,081
collected cases. Removed or consolidated 146 cases, without changing application
code, the collection configuration or skip conditions.

The largest reductions were configuration (56 to 31 test functions), Steam
(78 to 54), packaging metadata (25 to 7) and theme checks (14 to 1).
Configuration round trips now share one mixed-settings scenario, and Steam
parsing shares a nested fixture with comments, empty blocks and mixed casing.
Persistence failure, ownership recovery and asynchronous ordering remain separate.

Removed source snapshots for engine logging, GPU optimization implementation,
launcher command spelling and export callback spelling; copies of dependency
pins; README keyword checks; tests of the checker configuration and `.gitignore`;
cosmetic snapshots; duplicated helper checks; and the shader test that calculated
its own answer without running the shader. Removed their unused support code too.

The deleted modules were `test_engine_logging.py`, `test_low_resource_video.py`,
`test_gitignore.py` and `test_typechecking_config.py`. Git history retains them.
GPU patch behavior is still exercised by the native harnesses, but these are not
GPU pixel tests. Actual packaging and rendering still need the Flatpak build,
`tools/flatpak_smoke_test.py`, `tools/game_capture_frame_smoke.py` and visual checks
when those areas change. Passing Python tests alone does not establish those results.

## Retained coverage map

Each row describes why the module remains. Individual test names identify the
specific scenario within that contract. Similar cases in native and Python
process matching intentionally cover separate production implementations.

| Module (`test_` prefix, `.py` suffix) | Long-term regression protection |
| --- | --- |
| audio_source_discovery | Identify real playback applications and microphones without system-client noise. |
| audio_source_watcher | Refresh discovery for relevant audio events only. |
| audio_tracks_view | Preserve routing and offline selections, synchronize whitelist routes when games change, update gain live, release inactive discovery work. |
| autostart | Honor portal approval and native desktop-file ownership; report registration failures. |
| capture_modes | Migrate legacy capture labels and persist stable mode IDs. |
| clips_view | Rename and delete safely, retain drafts and game metadata, bound asynchronous gallery/cache work. |
| config | Persist typed settings, recover malformed files, migrate schemas and preserve engine-owned tokens. |
| copy_pacing | Execute capture pacing, lifecycle and wire-format checks in the C implementation. |
| dialogs | Expose the appropriate capture instructions and copy the actual launch option. |
| display_auth | Recover valid X11 authorization after session credentials change. |
| editor_drafts | Save, restore, rename and reject stale or malformed edits without losing usable history. |
| editor_export | Preserve aspect ratio, timing, audio and codec contracts; validate output despite AAC priming packet order and clean failed exports. |
| editor_export_dialog | Keep selected options, require overwrite confirmation, handle progress/cancel and block unsafe close. |
| editor_history | Maintain undo/redo branches, saved state and bounded/coalesced history. |
| editor_media | Preserve valid sample aspect ratio and reject invalid probe metadata. |
| editor_model | Maintain segment boundaries, time mappings and independent audio settings; preserve a final segment. |
| editor_preview | Keep cuts, seek/playback, live gain, decoder lifetime and stale callbacks consistent. |
| editor_timeline | Maintain click/playhead/zoom mappings, scroll limits and bounded waveform sampling. |
| editor_waveform | Preserve channel peaks and chunk boundaries; compare actual FFmpeg output across audio formats. |
| editor_window_shortcuts | Dispatch editor actions correctly, including physical keys under non-Latin layouts. |
| editor_window_timeline_interactions | Keep selection, scrubbing, gain editing, zoom anchoring and undo history consistent. |
| editor_window_transport | Coordinate seeking, volume, fullscreen and close lifecycle with the preview. |
| editor_window_wipe | Clear saved and in-memory edits only after confirmation; preserve them on cancellation. |
| engine_client | Encode IPC requests and complete saves once despite response/event ordering. |
| engine_ipc | Exercise the real engine socket, capabilities, recording lifecycle, saved files and concurrent clients. |
| engine_manager | Launch with the correct environment, forward diagnostics and distinguish restart from normal exit. |
| flatpak_metadata | Preserve desktop identity, icon policy, portable host helpers, pinned sources and narrow permissions. |
| focus_helpers | Preserve stable dropdown IDs, handle invalid selection and prevent controls stealing input. |
| game_audio_learning | Learn audio identity only from verified game processes and update the matching configuration. |
| game_capture | Publish/repair payloads atomically, preserve working copies and quote executable launch arguments. |
| game_capture_frame_smoke | Wait for actual save completion and fail promptly on engine errors/disconnects. |
| game_details | Replace executable identity safely, preserve user settings and resolve the correct Steam account. |
| game_icons | Find appropriate game artwork, avoid blocking discovery and bound extracted-icon caches. |
| hotkeys | Normalize valid chords, use physical keys and handle portal approval/cancellation races. |
| i18n | Discover catalogs, resolve locales, load the saved language early and validate translated UI coverage. |
| launchers | Reject shell syntax errors using the shell parser. |
| lazy_video_buffers | Execute allocation/reuse/failure cleanup against the shipped C patch helpers. |
| log_window | Stop subscriptions and scheduled refreshes after the window closes. |
| logs | Preserve useful diagnostics, bound history and carry logs through process handoff. |
| main | Coordinate startup/setup, tray lifecycle, recording, engine restarts, portal decisions, updates and session shutdown. |
| main_window_preferences | Pass catalog language IDs through preferences and track both physical Shift keys. |
| monitor_host | Execute C host process detection, fixed Steam operations, scope isolation and live rule reloads. |
| monitor_ipc | Validate incoming events and restrict socket access. |
| monitor_manager | Use the correct deployment/helper, validate host results and own one monitor process. |
| native_capture | Reject unsupported direct-capture scenes and preserve idle-render consumers. |
| pipewire_import_cache | Retain/release imported buffers and synchronization handles correctly, with and without explicit sync. |
| process_watcher | Match and deduplicate game identities across native, Steam, Wine and sandbox environments. |
| release_notes | Generate accurate categorized history from actual Git repositories and semantic tags. |
| release_version | Reject malformed tags and mismatched application release versions. |
| settings_view | Apply settings correctly, respect platform approval, handle capability races and clamp encoder controls. |
| setup_window | Persist setup choices, respect existing settings and complete or skip setup consistently. |
| shortcut_dialog | Validate accelerator syntax and present the correct shortcut collection for each window. |
| sound_feedback | Play the bundled asset with bounded gain and tolerate missing/unavailable media. |
| startup_benchmark | Measure the right processes and decode desktop presentation/handoff state accurately. |
| steam | Discover installations/accounts and change launch options without corrupting or claiming user-owned data. |
| suika_game_model | Keep drops in bounds and enforce fruit merge, score and terminal-size rules. |
| text_helpers | Preserve readable/full labels and escape user-controlled markup. |
| theme_adaptation | Parse editor CSS with GTK and reject errors. |
| tray | Maintain the public D-Bus/icon contract and flush replies before activation or shutdown. |
| update_controller | Coordinate checks, notifications, cancellation and restarts without interrupting saves or edits. |
| update_publication | Publish only complete release assets and prevent overwriting public releases. |
| updates | Reject invalid/untrusted updates, verify downloads and installation commits, prevent downgrades. |
| vaapi_direct_surfaces | Execute queue ownership, teardown, fence-wait and validation-cache C contracts. |
| video_output | Connect the color pipeline and fall back safely when GL elements or links fail. |
| video_player_window | Preserve playback/seek state, release media, size correctly and honor physical shortcuts. |
| whitelist_view | Persist scoped identities and present/search/remove the intended game. |
| window_state | Restore usable sizes after monitor changes and preserve other windows' saved state. |
