#!/usr/bin/env python3
"""Private Broadway renderer. Run through generate_screenshots.py."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tools/screenshot_fixture"


def render_language(work: Path, language: str, theme: str) -> None:
    """Import GTK only after setting locale, font discovery and private XDG paths."""
    session = work / f"{language}-{theme}"
    session.mkdir()
    for key, directory in (("XDG_CONFIG_HOME", "config"),
                           ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
        os.environ[key] = str(session / directory)
        (session / directory).mkdir()
    os.environ["FONTCONFIG_FILE"] = str(work / "fonts.conf")
    os.environ["CLIPPER_LOCALEDIR"] = str(work / "locale")
    sys.path[:0] = [str(REPO / "ui"), str(work / "vdf.whl")]
    from i18n import configure

    configure(language)
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    from gi.repository import Adw, Gio, GLib, Graphene, Gsk, Gtk

    Adw.init()
    settings = Gtk.Settings.get_default()
    appearance = json.loads((FIXTURE / "environment.json").read_text())["appearance"]
    settings.props.gtk_font_name = appearance["font_name"]
    settings.props.gtk_enable_animations = False
    settings.props.gtk_xft_dpi = 96 * 1024
    settings.props.gtk_xft_antialias = 1
    settings.props.gtk_xft_hinting = 1
    settings.props.gtk_xft_hintstyle = "hintslight"
    settings.props.gtk_xft_rgba = "none"
    settings.props.gtk_decoration_layout = appearance["decoration_layout"]
    style = Adw.StyleManager.get_default()
    style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if theme == "light"
                           else Adw.ColorScheme.FORCE_DARK)
    import app_icon

    app_icon._UI_ICON_RESOURCE_FILE = work / "icon-development-kit.gresource"
    app_icon._FLATPAK_ICON_DIR = work / "icons"
    app_icon.register_app_icon()
    data = json.loads((FIXTURE / "scene.json").read_text())
    from config import ClipperConfig

    games = data["whitelist"]
    for game in games:
        game["icon_path"] = str(FIXTURE / game["icon_path"])
    config_path = session / "config/clipper/config.json"
    config_path.parent.mkdir()
    folder = session / "clips"
    folder.mkdir()
    config_path.write_text(json.dumps({
        "setup_completed": True, "ui_language": language, "whitelist": games,
        "audio": data["audio"], "output_folder": str(folder),
        "remember_window_sizes": False, "auto_check_updates": False,
    }))
    config = ClipperConfig(config_path)
    # Keep the production filesystem scan, size/date formatting, cache lookup,
    # localization and row construction. Sparse stand-ins cost no video storage.
    import clips_view
    import steam

    class SceneClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat(data["clock"])
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    clips_view.datetime = SceneClock
    steam.get_game_header_path = lambda appid: str(FIXTURE / next(
        game["artwork"] for game in games if game["appid"] == appid
    ))
    clip_metadata = {}
    media_metadata = {}
    clips_view.THUMBNAIL_DIR.mkdir(parents=True)
    for clip in data["clips"]:
        path = folder / clip["name"]
        with path.open("wb") as stand_in:
            stand_in.truncate(clip["size"])
        os.utime(path, ns=(clip["mtime_ns"], clip["mtime_ns"]))
        game = next(game for game in games if game["appid"] == clip["appid"])
        clip_metadata[str(path)] = {
            "path": str(path), "mtime_ns": clip["mtime_ns"], "size": clip["size"],
            "game": game,
        }
        thumbnail = clips_view.ClipsView._thumbnail_path_for_identity(
            None, path, clip["mtime_ns"], clip["size"]
        )
        shutil.copyfile(FIXTURE / clip["thumbnail"], thumbnail)
        seconds = clip["duration_us"] // 1_000_000
        media_metadata[str(path)] = {
            "mtime_ns": clip["mtime_ns"], "size": clip["size"],
            "duration": f"{seconds // 60}:{seconds % 60:02d}", "thumbnail": thumbnail.name,
        }
    config.set("clip_game_metadata", {"clips": clip_metadata})
    clips_view.MEDIA_METADATA_CACHE_FILE.write_text(json.dumps({
        "version": clips_view.MEDIA_METADATA_CACHE_VERSION, "clips": media_metadata,
    }))
    import audio_tracks_view

    audio_tracks_view.list_runtime_audio_sources = lambda: data["audio_sources"]
    # Only external discovery is replaced. The real Audio view builds its
    # selectors and localized labels from the saved configuration.
    audio_tracks_view.AudioSourceWatcher.start = lambda _watcher: None
    from icon_names import APP_ID
    from main_window import MainWindow

    # GTK uses the application ID to find the header bar's app icon.
    # NON_UNIQUE and the private session bus keep this separate from real Clipper.
    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    presentable = set()
    window = MainWindow(app, config=config, presentation_callback=presentable.add)
    # Broadway's virtual monitor can constrain windows. Preserve the defaults
    # chosen by the actual UI constructor instead of duplicating fixture sizes.
    window.set_size_request(*window.get_default_size())
    window.present()
    loop = GLib.MainLoop()
    failure = []

    def snapshot(widget, scene):
        snapshot = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(widget).snapshot(snapshot, widget.get_width(), widget.get_height())
        node = snapshot.to_node()
        if node is None:
            return False
        renderer = Gsk.CairoRenderer.new()
        renderer.realize(widget.get_surface())
        try:
            bounds = Graphene.Rect()
            bounds.init(0, 0, widget.get_width(), widget.get_height())
            texture = renderer.render_texture(node, bounds)
            target = work / "output" / language / f"{scene}-{theme}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not texture.save_to_png(str(target)):
                raise RuntimeError(f"Cannot save {target}")
        finally:
            renderer.unrealize()
        return True

    def later(callback):
        def invoke():
            try:
                callback()
            except Exception as error:
                failure.append(error)
                loop.quit()
            return False
        GLib.timeout_add(250, invoke)

    def capture_clips():
        if "clips" not in presentable:
            later(capture_clips)
            return
        if not snapshot(window, "clips"):
            later(capture_clips)
            return
        window.view_stack.set_visible_child_name("whitelist")
        later(capture_games)

    def capture_games():
        if not snapshot(window, "games"):
            later(capture_games)
            return
        window.view_stack.set_visible_child_name("audio")
        later(capture_audio)

    def capture_audio():
        if not snapshot(window, "audio"):
            later(capture_audio)
            return
        window.cleanup()
        window.destroy()
        capture_editor()

    def capture_editor():
        import editor_waveform
        import editor_window
        from editor_model import EditorProject

        editor_data = data["editor"]
        project = EditorProject.from_dict(editor_data["project"])
        project.source.path = str(folder / Path(project.source.path).name)
        source_frame = project.output_to_source_us(editor_data["playhead_us"])
        if source_frame != editor_data["source_frame_us"]:
            raise RuntimeError("The editor frame no longer matches the saved draft playhead")
        raw = zlib.decompress((FIXTURE / "waveforms.bin.z").read_bytes())
        waveforms = {}
        offset = 0
        for track, count in zip(project.source.audio_tracks, editor_data["waveform_buckets"],
                                strict=True):
            end = offset + count * 4
            waveforms[track.id] = list(struct.iter_unpack("<hh", raw[offset:end]))
            offset = end
        if offset != len(raw):
            raise RuntimeError("Unexpected waveform fixture length")
        editor_waveform.generate_waveform = lambda _source, track, _cancel: waveforms[track.id]

        class StillPreview(editor_window.EditorPreview):
            """Replace decoder I/O while inheriting the actual preview UI and setup."""

            def _build_pipeline(self):
                self.picture.set_filename(str(FIXTURE / "editor-frame.jpg"))

            def update_project(self, _project):
                pass

            def seek_output(self, output_us):
                self.output_position_us = output_us
                if self.changed_callback is not None:
                    self.changed_callback(output_us)

            def finish_seek(self):
                pass

            def cleanup(self):
                pass

        editor_window.EditorPreview = StillPreview
        editor = editor_window.EditorWindow(app, project, config, lambda _project: None)
        editor.set_size_request(*editor.get_default_size())
        selected = project.segments[editor_data["selected_segment"]].id
        editor._set_selection({selected}, selected)
        editor._refresh(update_preview=False)
        editor.present()

        def set_editor_view():
            editor.set_focus(None)
            editor.preview.seek_output(editor_data["playhead_us"])
            editor.zoom_scale.set_value(editor_data["zoom_percent"])
            editor.timeline_scroll.get_hadjustment().set_value(0)
            later(finish_editor)

        def finish_editor():
            if len(editor._waveforms) != len(project.source.audio_tracks):
                later(finish_editor)
                return
            if editor._playhead_us != 2_000_000 or editor._zoom != 2.25:
                raise RuntimeError("Editor screenshot must be at 2 seconds and 225% zoom")
            if not snapshot(editor, "editor"):
                later(finish_editor)
                return
            editor.cleanup()
            editor.destroy()
            loop.quit()

        later(set_editor_view)

    def expired():
        failure.append(RuntimeError(f"Screenshot rendering timed out: {language}/{theme}"))
        loop.quit()
        return False

    GLib.timeout_add_seconds(15, expired)
    window.set_focus(None)
    later(capture_clips)
    loop.run()
    if failure:
        raise failure[0]
    print(f"Rendered {language}/{theme}: games, editor, audio, clips", flush=True)


def main() -> None:
    work = Path(sys.argv[1])
    if len(sys.argv) == 5 and sys.argv[2] == "--render":
        render_language(work, sys.argv[3], sys.argv[4])
        return
    (work / "fonts.conf").write_text(f'''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <include>/etc/fonts/fonts.conf</include>
  <reset-dirs/>
  <dir>{work / 'fonts'}</dir>
  <dir>/usr/share/fonts/dejavu</dir>
  <cachedir>{work / 'font-cache'}</cachedir>
  <alias><family>sans-serif</family><prefer><family>Noto Sans</family></prefer></alias>
  <alias><family>monospace</family><prefer><family>DejaVu Sans Mono</family></prefer></alias>
  <match target="font"><edit name="hintstyle" mode="assign"><const>hintslight</const></edit></match>
  <match target="font"><edit name="rgba" mode="assign"><const>none</const></edit></match>
</fontconfig>
''')
    xml = work / "icon-library/data/icons.gresource.xml"
    xml.write_text(xml.read_text().replace("/org/gnome/design/IconLibrary/icons",
                                          "/io/github/leesethefox/Clipper/icons"))
    subprocess.run(["glib-compile-resources", str(xml),
                    f"--sourcedir={xml.parent}",
                    f"--target={work / 'icon-development-kit.gresource'}"], check=True)
    subprocess.run(["python3", str(REPO / "tools/build_translations.py"),
                    "--output-dir", str(work / "locale")], check=True)
    server = subprocess.Popen(["gtk4-broadwayd", ":1"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
    try:
        # The server creates its private Unix socket before the first GTK import.
        deadline = time.monotonic() + 5
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        while not (runtime / "broadway2.socket").exists():
            if server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Cannot start the private GTK Broadway display")
            time.sleep(.02)
        for language in sys.argv[2:]:
            for theme in ("light", "dark"):
                subprocess.run(["python3", __file__, str(work), "--render", language, theme],
                               check=True, timeout=25)
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    main()
