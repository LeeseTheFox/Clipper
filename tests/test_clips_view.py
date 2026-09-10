import importlib.util
import sys
import types
from pathlib import Path


def _load_clips_module():
    module_name = "test_clips_view_isolated"
    module_path = Path(__file__).resolve().parents[1] / "ui" / "clips_view.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    original_modules = {
        name: sys.modules.get(name)
        for name in (
            "gi",
            "gi.repository",
            "config",
            "game_icons",
        )
    }

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args, **_kwargs: None

    repository = types.ModuleType("gi.repository")
    repository.Adw = types.SimpleNamespace(
        MessageDialog=types.SimpleNamespace(new=lambda *_args: None),
        ResponseAppearance=types.SimpleNamespace(DESTRUCTIVE=1),
    )
    repository.Gdk = types.SimpleNamespace(
        Display=types.SimpleNamespace(get_default=lambda: None),
        ModifierType=types.SimpleNamespace(SHIFT_MASK=1),
    )

    class StringObjectStub:
        def __init__(self, value):
            self.value = value

        def get_string(self):
            return self.value

    class StringListStub:
        def __init__(self, values):
            self.values = list(values)

        @classmethod
        def new(cls, values):
            return cls(values)

        def get_n_items(self):
            return len(self.values)

        def get_item(self, index):
            return StringObjectStub(self.values[index])

    class NoSelectionStub:
        def __init__(self, model):
            self.model = model

        @classmethod
        def new(cls, model):
            return cls(model)

    class BoxStub:
        def __init__(self, **_kwargs):
            self.size_request = None

        def set_size_request(self, width, height):
            self.size_request = (width, height)

    repository.Gio = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace(
        idle_add=lambda callback, *args: callback(*args),
        timeout_add=lambda _delay, callback, *args: callback(*args),
    )
    repository.Gtk = types.SimpleNamespace(
        Align=types.SimpleNamespace(CENTER=1, START=2),
        Box=BoxStub,
        Button=types.SimpleNamespace(new_from_icon_name=lambda *_args: None),
        CssProvider=type("CssProvider", (), {}),
        GestureClick=type("GestureClick", (), {}),
        Image=types.SimpleNamespace(
            new_from_file=lambda *_args: None,
            new_from_icon_name=lambda *_args: None,
        ),
        Label=types.SimpleNamespace(),
        ListBox=type("ListBox", (), {}),
        ListBoxRow=type("ListBoxRow", (), {}),
        Orientation=types.SimpleNamespace(HORIZONTAL=1, VERTICAL=2),
        Overflow=types.SimpleNamespace(HIDDEN=1),
        Picture=types.SimpleNamespace(new_for_filename=lambda *_args: None),
        PolicyType=types.SimpleNamespace(NEVER=1, AUTOMATIC=2),
        STYLE_PROVIDER_PRIORITY_APPLICATION=1,
        ScrolledWindow=type("ScrolledWindow", (), {}),
        SelectionMode=types.SimpleNamespace(NONE=0),
        StringList=StringListStub,
        NoSelection=NoSelectionStub,
        StyleContext=types.SimpleNamespace(add_provider_for_display=lambda *_args: None),
    )
    gi.repository = repository

    config = types.ModuleType("config")
    config.ClipperConfig = type("ClipperConfig", (), {})
    game_icons = types.ModuleType("game_icons")
    game_icons.cached_icon_path_for_game = lambda game_data: game_data.get("icon_path", "")
    game_icons.submit_icon_resolution = lambda _game_data: None

    sys.modules.update(
        {
            "gi": gi,
            "gi.repository": repository,
            "config": config,
            "game_icons": game_icons,
        }
    )

    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


clips_module = _load_clips_module()
ClipsView = clips_module.ClipsView


def test_clips_rendered_callback_is_optional_and_invoked_when_present():
    view = object.__new__(ClipsView)
    view._notify_clips_rendered()

    calls = []
    view.clips_rendered_callback = lambda: calls.append(True)
    view._notify_clips_rendered()

    assert calls == [True]


def test_cache_directory_honors_xdg_cache_home():
    assert clips_module._default_cache_dir({"XDG_CACHE_HOME": "/cache-root"}) == Path(
        "/cache-root/clipper"
    )


def test_thumbnail_placeholder_expands_to_center_within_frame(monkeypatch):
    class ImageStub:
        def __init__(self):
            self.pixel_size = None
            self.hexpand = None
            self.vexpand = None
            self.halign = None
            self.valign = None

        def set_pixel_size(self, value):
            self.pixel_size = value

        def set_hexpand(self, value):
            self.hexpand = value

        def set_vexpand(self, value):
            self.vexpand = value

        def set_halign(self, value):
            self.halign = value

        def set_valign(self, value):
            self.valign = value

    image = ImageStub()
    container = types.SimpleNamespace(
        set_hexpand=lambda value: setattr(container, "hexpand", value),
        get_first_child=lambda: None,
        append=lambda child: setattr(container, "child", child),
    )
    monkeypatch.setattr(clips_module.Gtk.Image, "new_from_icon_name", lambda _name: image)

    object.__new__(ClipsView)._set_clip_thumbnail_widget(container, None)

    assert container.child is image
    assert image.pixel_size == 32
    assert image.hexpand is True
    assert image.vexpand is True
    assert image.halign == clips_module.Gtk.Align.CENTER
    assert image.valign == clips_module.Gtk.Align.CENTER
    assert container.hexpand is False


def test_thumbnail_play_hover_replaces_placeholder_without_overlap():
    class ContentStub:
        def set_opacity(self, value):
            self.opacity = value

    class ButtonStub:
        def set_visible(self, value):
            self.visible = value

    content = ContentStub()
    container = types.SimpleNamespace(
        _clipper_has_thumbnail=False,
        get_first_child=lambda: content,
    )
    button = ButtonStub()

    ClipsView._set_thumbnail_play_hover(container, button, True)

    assert content.opacity == 0
    assert button.visible is True

    ClipsView._set_thumbnail_play_hover(container, button, False)

    assert content.opacity == 1
    assert button.visible is False


def test_thumbnail_play_hover_keeps_real_thumbnail_visible():
    content = types.SimpleNamespace(set_opacity=lambda value: setattr(content, "opacity", value))
    container = types.SimpleNamespace(
        _clipper_has_thumbnail=True,
        get_first_child=lambda: content,
    )
    button = types.SimpleNamespace(set_visible=lambda value: setattr(button, "visible", value))

    ClipsView._set_thumbnail_play_hover(container, button, True)

    assert content.opacity == 1
    assert button.visible is True


def test_play_clip_launches_isolated_native_player(monkeypatch, tmp_path):
    popen_calls = []
    process = types.SimpleNamespace()
    monkeypatch.setattr(
        clips_module.subprocess,
        "Popen",
        lambda args, **kwargs: popen_calls.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(clips_module.os, "getpid", lambda: 4321)
    view = object.__new__(ClipsView)
    view._player_process = None
    clip_path = tmp_path / "Game_2026-08-13_12-00-00.mkv"

    view.on_play_clip({"path": clip_path, "name": clip_path.name})

    assert view._player_process is process
    args, kwargs = popen_calls[0]
    module_file = clips_module.__file__
    assert module_file is not None
    assert args == [
        clips_module.sys.executable,
        str(Path(module_file).with_name("video_player_window.py")),
        str(clip_path),
        "--title",
        clip_path.name,
        "--parent-pid",
        "4321",
    ]
    assert kwargs == {"close_fds": True}


def test_play_clip_exports_wayland_parent_for_compositor_placement(monkeypatch, tmp_path):
    events = []

    class SurfaceStub:
        def export_handle(self, callback, user_data):
            events.append("export")
            callback(self, "clipper-parent-handle", user_data)
            return True

        def drop_exported_handle(self, handle):
            events.append(("drop", handle))

    surface = SurfaceStub()
    process = types.SimpleNamespace(pid=9876)
    popen_calls = []
    watched = []
    monkeypatch.setattr(
        clips_module,
        "GdkWayland",
        types.SimpleNamespace(WaylandToplevel=SurfaceStub),
    )
    monkeypatch.setattr(
        clips_module.subprocess,
        "Popen",
        lambda args, **kwargs: popen_calls.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(clips_module.os, "getpid", lambda: 4321)
    monkeypatch.setattr(clips_module.GLib, "PRIORITY_DEFAULT", 0, raising=False)
    monkeypatch.setattr(
        clips_module.GLib,
        "child_watch_add",
        lambda priority, pid, callback: watched.append((priority, pid, callback)),
        raising=False,
    )
    monkeypatch.setattr(clips_module.GLib, "spawn_close_pid", lambda pid: None, raising=False)
    view = object.__new__(ClipsView)
    view._player_process = None
    view._player_launch_generation = 0
    view._exported_player_parents = {}
    view.get_root = lambda: types.SimpleNamespace(get_surface=lambda: surface)
    clip_path = tmp_path / "centered.mkv"

    view.on_play_clip({"path": clip_path, "name": clip_path.name})

    args, kwargs = popen_calls[0]
    assert args[-2:] == ["--parent-handle", "clipper-parent-handle"]
    assert kwargs == {"close_fds": True}
    assert view._exported_player_parents == {
        9876: (surface, "clipper-parent-handle")
    }
    assert watched[0][:2] == (0, 9876)

    watched[0][2](9876, 0)
    assert events == ["export", ("drop", "clipper-parent-handle")]


def test_play_clip_terminates_previous_isolated_player(monkeypatch, tmp_path):
    events = []
    previous = types.SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
    )
    replacement = object()
    monkeypatch.setattr(
        clips_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: events.append("launch") or replacement,
    )
    view = object.__new__(ClipsView)
    view._player_process = previous
    clip_path = tmp_path / "another-clip.mp4"

    view.on_play_clip({"path": clip_path, "name": clip_path.name})

    assert view._player_process is replacement
    assert events == ["terminate", "launch"]


class ListStub:
    def __init__(self):
        self.removed = []
        self.rows = []

    def append(self, row):
        self.rows.append(row)

    def get_row_at_index(self, index):
        if index < len(self.rows):
            return self.rows[index]
        return None

    def remove(self, row):
        self.removed.append(row)
        if row in self.rows:
            self.rows.remove(row)

    def set_model(self, model):
        self.model = model


class ScrolledStub:
    def __init__(self):
        self.child = None

    def set_child(self, child):
        self.child = child


class ListItemStub:
    def __init__(self, item=None):
        self.item = item
        self.child = None

    def get_item(self):
        return self.item

    def get_child(self):
        return self.child

    def set_child(self, child):
        self.child = child


class ThreadStub:
    created = []

    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


class GioFileStub:
    def __init__(self, path):
        self._path = str(path)

    def get_path(self):
        return self._path


class ConfigStub:
    def __init__(self, data=None):
        self.data = data or {}
        self.set_calls = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value
        self.set_calls.append((key, value))


def _make_view():
    view = object.__new__(ClipsView)
    view.clips_list = ListStub()
    view.scrolled = ScrolledStub()
    view.empty_state = object()
    view.no_results_state = object()
    view.clips_data = []
    view.search_query = ""
    view.sort_mode = ClipsView.SORT_NEWEST
    view._loading = False
    view._media_load_generation = 0
    view._media_enrichment_start_id = None
    view._media_cache_save_id = None
    view._media_enrichment_running = False
    view._media_pending_jobs = {}
    view._media_inflight_paths = set()
    view._media_metadata = {}
    view._clip_identities = {}
    view._clips_by_path = {}
    view._media_widgets = {}
    view._visible_clip_data = []
    view._clip_string_model = None
    view._clip_selection_model = None
    view._bound_clip_paths = set()
    view._clips_rendered_pending = False
    view._locally_deleted_paths = set()
    view._local_delete_forget_ids = {}
    view._edit_buttons = set()
    view._delete_buttons = set()
    view._shift_edit_mode = False
    view.refresh_called = False
    view.refresh = lambda **_kwargs: setattr(view, "refresh_called", True)
    view._show_toast = lambda _message: None
    view.create_clip_row = lambda clip: ("row", clip["path"])
    view._schedule_media_cache_save = lambda: None
    view.config = ConfigStub({"whitelist": [], "clip_game_metadata": {"clips": {}}})
    return view


def test_clip_display_name_hides_only_final_extension():
    view = _make_view()
    assert view._clip_display_name(Path("/clips/My.clip.mkv")) == "My.clip"


def test_clip_rename_updates_filename_and_indexes(tmp_path):
    view = _make_view()
    path = tmp_path / "clip.mkv"
    path.write_bytes(b"video")
    clip = {"path": path, "name": "clip"}
    assert view._save_clip_name(clip, "Мій кліп 🎮.final")
    renamed_path = tmp_path / "Мій кліп 🎮.final.mkv"
    assert clip["path"] == renamed_path
    assert not path.exists()
    assert renamed_path.read_bytes() == b"video"
    reloaded = _make_view()
    reloaded.config = ConfigStub(view.config.data.copy())
    assert reloaded._clip_display_name(renamed_path) == "Мій кліп 🎮.final"
    assert clip["name"] == "Мій кліп 🎮.final"
    view.clips_data = [clip]
    view.search_query = "Мій"
    assert view._visible_clips() == [clip]


def test_clip_rename_rejects_existing_filename(tmp_path):
    view = _make_view()
    path = tmp_path / "clip.mkv"
    target = tmp_path / "other.mkv"
    path.write_bytes(b"video")
    target.write_bytes(b"other")
    clip = {"path": path, "name": "clip"}
    assert not view._save_clip_name(clip, "other")
    assert clip["path"] == path
    assert path.read_bytes() == b"video"
    assert target.read_bytes() == b"other"


def test_invalid_clip_names_do_not_replace_saved_name():
    view = _make_view()
    clip = {"path": Path("/clips/clip.mkv"), "name": "clip"}
    for name in ("", "  ", ".", "..", "a/b", "a\0b", "a\nb"):
        assert not view._save_clip_name(clip, name)
    assert clip["name"] == "clip"
    assert not view.config.set_calls


def test_clip_name_metadata_save_failure_keeps_renamed_file(tmp_path):
    view = _make_view()
    path = tmp_path / "clip.mkv"
    path.write_bytes(b"video")
    clip = {"path": path, "name": "clip"}

    def fail(*_args):
        raise OSError("disk full")

    view.config.set = fail
    assert view._save_clip_name(clip, "new title")
    assert clip["name"] == "new title"
    assert clip["path"] == tmp_path / "new title.mkv"


def test_inline_name_focus_waits_until_pointer_dispatch_finishes(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    widgets = {}
    for name in ("Stack", "Label", "Text", "EventControllerFocus", "EventControllerKey"):
        widgets[name] = MagicMock()
        monkeypatch.setattr(
            clips_module.Gtk, name, MagicMock(return_value=widgets[name]), raising=False
        )
    outside, gesture = MagicMock(), MagicMock()
    monkeypatch.setattr(
        clips_module.Gtk, "GestureClick", MagicMock(side_effect=[outside, gesture])
    )
    for name in ("PropagationPhase", "EventSequenceState"):
        monkeypatch.setattr(clips_module.Gtk, name, MagicMock(), raising=False)
    pending = []
    monkeypatch.setattr(clips_module.GLib, "idle_add", lambda callback: pending.append(callback))
    view = _make_view()
    path = tmp_path / "clip.mkv"
    path.write_bytes(b"video")
    clip = {"path": path, "name": "clip"}
    view._create_clip_name(clip)
    pressed = gesture.connect.call_args.args[1]
    pressed(gesture, 1, 0, 0)
    assert not pending
    pressed(gesture, 2, 0, 0)
    widgets["Text"].grab_focus.assert_not_called()
    assert len(pending) == 1
    assert pending.pop()() is False
    widgets["Text"].grab_focus.assert_called_once()
    widgets["Text"].set_position.assert_called_once_with(-1)
    widgets["Text"].get_text.return_value = "Edited clip"
    activated = widgets["Text"].connect.call_args.args[1]
    activated(widgets["Text"])
    assert clip["name"] == "Edited clip"
    widgets["Stack"].set_visible_child_name.assert_called_with("label")


def test_inline_name_escape_saves_and_closes_editor(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    widgets = {}
    for name in ("Stack", "Label", "Text", "EventControllerFocus", "EventControllerKey"):
        widgets[name] = MagicMock()
        monkeypatch.setattr(
            clips_module.Gtk, name, MagicMock(return_value=widgets[name]), raising=False
        )
    outside, gesture = MagicMock(), MagicMock()
    monkeypatch.setattr(clips_module.Gtk, "GestureClick", MagicMock(side_effect=[outside, gesture]))
    monkeypatch.setattr(clips_module.Gtk, "PropagationPhase", MagicMock(), raising=False)
    monkeypatch.setattr(clips_module.Gtk, "EventSequenceState", MagicMock(), raising=False)
    monkeypatch.setattr(clips_module.GLib, "idle_add", lambda callback: callback())
    monkeypatch.setattr(clips_module.Gdk, "KEY_Escape", 65307, raising=False)
    view = _make_view()
    path = tmp_path / "clip.mkv"
    path.write_bytes(b"video")
    clip = {"path": path, "name": "clip"}
    view._create_clip_name(clip)
    pressed = gesture.connect.call_args.args[1]
    pressed(gesture, 2, 0, 0)
    widgets["Text"].get_text.return_value = "Edited clip"
    key_handler = widgets["EventControllerKey"].connect.call_args.args[1]
    assert key_handler(widgets["EventControllerKey"], 65307, 0, 0)
    assert clip["name"] == "Edited clip"
    widgets["Stack"].set_visible_child_name.assert_called_with("label")


def test_clip_list_allows_its_inline_editor_to_receive_focus(monkeypatch):
    from unittest.mock import MagicMock

    gtk = MagicMock()
    monkeypatch.setattr(clips_module, "Gtk", gtk)
    monkeypatch.setattr(clips_module, "_install_thumbnail_css", lambda: None)
    monkeypatch.setattr(clips_module, "new_id_dropdown", MagicMock())
    view = _make_view()
    view.append = MagicMock()
    view.create_empty_state = MagicMock()
    view.create_no_results_state = MagicMock()
    view.create_loading_state = MagicMock()
    view.setup_ui()

    # can-focus=False disables the entire subtree, so even a successful
    # double-click cannot put keyboard focus in the name editor.
    view.clips_list.set_can_focus.assert_not_called()
    view.clips_list.set_focusable.assert_called_once_with(False)
    item = MagicMock()
    view._on_clip_item_setup(None, item)
    item.set_focusable.assert_called_once_with(False)
    item.set_child.assert_called_once()


def test_initial_clip_load_does_not_run_media_tools(monkeypatch, tmp_path):
    clip_path = tmp_path / "clip.mkv"
    clip_path.write_bytes(b"clip")
    monkeypatch.setattr(clips_module, "MEDIA_METADATA_CACHE_FILE", tmp_path / "media-cache.json")
    view = _make_view()
    view.config = ConfigStub(
        {
            "output_folder": str(tmp_path),
            "whitelist": [],
            "clip_game_metadata": {"clips": {}},
        }
    )
    queued = []
    view._queue_media_for_clips = lambda clips: queued.extend(clips)
    monkeypatch.setattr(
        view,
        "get_video_duration",
        lambda _path: (_ for _ in ()).throw(AssertionError("ffprobe ran on startup")),
    )
    monkeypatch.setattr(
        view,
        "get_video_thumbnail",
        lambda _path, _stat: (_ for _ in ()).throw(AssertionError("ffmpeg ran on startup")),
    )

    view.load_clips()

    assert [clip["path"] for clip in view.clips_data] == [clip_path]
    assert view.clips_list.model.model.values == [str(clip_path)]
    assert view.clips_data[0]["duration"] is None
    assert view.clips_data[0]["thumbnail"] is None
    assert queued == []

    item = ListItemStub(view._clip_string_model.get_item(0))
    view._on_clip_item_bind(None, item)

    assert item.child == ("row", clip_path)
    assert [clip["path"] for clip in queued] == [clip_path]


def test_initial_load_of_hundreds_of_clips_only_builds_lightweight_model(monkeypatch, tmp_path):
    for index in range(500):
        (tmp_path / f"clip-{index}.mkv").write_bytes(b"clip")
    monkeypatch.setattr(clips_module, "MEDIA_METADATA_CACHE_FILE", tmp_path / "media-cache.json")
    monkeypatch.setattr(clips_module, "THUMBNAIL_DIR", tmp_path / "thumbnails")
    view = _make_view()
    view.config = ConfigStub(
        {
            "output_folder": str(tmp_path),
            "whitelist": [],
            "clip_game_metadata": {"clips": {}},
        }
    )
    queued = []
    view._queue_media_for_clips = lambda clips: queued.extend(clips)

    view.load_clips()

    assert len(view.clips_data) == 500
    assert view._clip_string_model.get_n_items() == 500
    assert view.clips_list.rows == []
    assert queued == []


def test_media_duration_cache_is_validated_by_file_identity(monkeypatch, tmp_path):
    cache_file = tmp_path / "cache" / "media-metadata.json"
    monkeypatch.setattr(clips_module, "MEDIA_METADATA_CACHE_FILE", cache_file)
    monkeypatch.setattr(clips_module, "THUMBNAIL_DIR", tmp_path / "thumbnails")
    clip_path = tmp_path / "clip.mkv"
    clip_path.write_bytes(b"clip")
    view = _make_view()
    stat = clip_path.stat()

    view._save_media_metadata_cache({str(clip_path): view._media_metadata_entry(stat, "1:23")})
    metadata = view._load_media_metadata_cache()

    assert view._cached_video_duration(clip_path, stat, metadata) == "1:23"
    clip_path.write_bytes(b"changed clip")
    assert view._cached_video_duration(clip_path, clip_path.stat(), metadata) is None


def test_media_enrichment_probes_off_main_path_and_returns_to_glib(monkeypatch, tmp_path):
    clip_path = tmp_path / "clip.mkv"
    clip_path.write_bytes(b"clip")
    stat = clip_path.stat()
    thumbnail_path = tmp_path / "thumbnail.jpg"
    view = _make_view()
    callbacks = []
    monkeypatch.setattr(view, "get_video_duration", lambda _path: "0:42")
    monkeypatch.setattr(view, "get_video_thumbnail", lambda _path, _stat: thumbnail_path)
    monkeypatch.setattr(
        clips_module.GLib,
        "idle_add",
        lambda callback, *args: callbacks.append((callback, args)),
    )

    view._enrich_media_worker(
        7,
        [
            {
                "path": clip_path,
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "duration": None,
                "thumbnail": None,
            }
        ],
    )

    assert callbacks == [
        (
            view._apply_media_enrichment,
            (
                7,
                {
                    str(clip_path): {
                        "duration": "0:42",
                        "thumbnail": None,
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                    }
                },
            ),
        ),
        (
            view._apply_media_enrichment,
            (
                7,
                {
                    str(clip_path): {
                        "duration": "0:42",
                        "thumbnail": thumbnail_path,
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                    }
                },
            ),
        ),
        (view._on_media_enrichment_batch_finished, (7, (str(clip_path),))),
    ]


def test_large_library_recycles_bound_row_widgets(tmp_path):
    view = _make_view()
    view.clips_data = [
        {
            "path": tmp_path / f"clip-{index}.mkv",
            "name": f"Clip {index:03}.mkv",
            "mtime": 1_000 - index,
        }
        for index in range(300)
    ]
    view._clips_by_path = {str(clip["path"]): clip for clip in view.clips_data}
    view._queue_media_for_clips = lambda _clips: None
    created = 0

    def create_row(clip):
        nonlocal created
        created += 1
        return ("row", clip["path"])

    view.create_clip_row = create_row

    view._render_clips()

    assert len(view._visible_clip_data) == 300
    assert view._clip_string_model.get_n_items() == 300
    assert created == 0

    list_item = ListItemStub()
    for index in range(300):
        list_item.item = view._clip_string_model.get_item(index)
        view._on_clip_item_bind(None, list_item)
        assert len(view._bound_clip_paths) == 1
        assert list_item.child == ("row", view._visible_clip_data[index]["path"])
        view._on_clip_item_unbind(None, list_item)
        assert view._bound_clip_paths == set()
        assert list_item.child is not None
        assert list_item.child.size_request == (
            -1,
            clips_module.CLIP_ROW_CONTENT_HEIGHT,
        )

    assert created == 300


def test_unbinding_virtualized_row_discards_media_work_not_yet_started(tmp_path):
    view = _make_view()
    path = str(tmp_path / "clip.mkv")
    view._bound_clip_paths.add(path)
    view._media_pending_jobs[path] = {"path": Path(path)}
    list_item = ListItemStub()
    list_item._clipper_path = path
    list_item.child = object()

    view._on_clip_item_unbind(None, list_item)

    assert path not in view._bound_clip_paths
    assert path not in view._media_pending_jobs
    assert list_item.child.size_request == (-1, clips_module.CLIP_ROW_CONTENT_HEIGHT)


def test_media_worker_processes_a_bounded_job_batch(monkeypatch, tmp_path):
    class CapturedThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            pass

    view = _make_view()
    view._media_pending_jobs = {
        str(tmp_path / f"clip-{index}.mkv"): {"path": index} for index in range(20)
    }
    monkeypatch.setattr(clips_module.threading, "Thread", CapturedThread)

    view._start_media_enrichment_worker()

    assert view._media_enrichment_running is True
    assert len(view._media_pending_jobs) == 20 - clips_module.MEDIA_ENRICHMENT_BATCH_SIZE
    assert len(view._media_inflight_paths) == clips_module.MEDIA_ENRICHMENT_BATCH_SIZE


def test_cache_maintenance_waits_for_media_worker(monkeypatch):
    view = _make_view()
    view._media_enrichment_running = True
    scheduled = []
    monkeypatch.setattr(
        clips_module.GLib,
        "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 17,
    )
    monkeypatch.setattr(
        clips_module.threading,
        "Thread",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache worker started during media enrichment")
        ),
    )

    view._start_media_cache_save()

    assert scheduled == [(clips_module.MEDIA_CACHE_SAVE_DELAY_MS, view._start_media_cache_save)]
    assert view._media_cache_save_id == 17


def test_media_metadata_cache_is_bounded_to_newest_entries():
    view = _make_view()
    count = clips_module.MAX_MEDIA_METADATA_ENTRIES + 25
    metadata = {
        f"/clips/{index}.mkv": {
            "mtime_ns": index,
            "size": index + 1,
            "duration": "0:01",
        }
        for index in range(count)
    }
    identities = {path: (entry["mtime_ns"], entry["size"]) for path, entry in metadata.items()}

    pruned = view._pruned_media_metadata(metadata, identities)

    assert len(pruned) == clips_module.MAX_MEDIA_METADATA_ENTRIES
    assert "/clips/0.mkv" not in pruned
    assert f"/clips/{count - 1}.mkv" in pruned


def test_media_metadata_prunes_deleted_and_changed_clips():
    view = _make_view()
    metadata = {
        "/clips/current.mkv": {"mtime_ns": 10, "size": 20, "duration": "0:01"},
        "/clips/changed.mkv": {"mtime_ns": 11, "size": 21, "duration": "0:02"},
        "/clips/deleted.mkv": {"mtime_ns": 12, "size": 22, "duration": "0:03"},
    }
    identities = {
        "/clips/current.mkv": (10, 20),
        "/clips/changed.mkv": (99, 21),
    }

    assert view._pruned_media_metadata(metadata, identities) == {
        "/clips/current.mkv": metadata["/clips/current.mkv"]
    }


def test_thumbnail_cache_removes_orphans_and_obeys_count_limit(monkeypatch, tmp_path):
    thumbnail_dir = tmp_path / "thumbnails"
    thumbnail_dir.mkdir()
    monkeypatch.setattr(clips_module, "THUMBNAIL_DIR", thumbnail_dir)
    monkeypatch.setattr(clips_module, "MAX_THUMBNAIL_CACHE_ENTRIES", 2)
    view = _make_view()
    identities = {}
    expected_paths = []
    for index in range(3):
        clip_path = tmp_path / f"clip-{index}.mkv"
        identity = (index + 1, 100 + index)
        identities[str(clip_path)] = identity
        thumbnail = view._thumbnail_path_for_identity(clip_path, *identity)
        thumbnail.write_bytes(b"\xff\xd8thumbnail")
        expected_paths.append(thumbnail)
    orphan = thumbnail_dir / "orphan.jpg"
    orphan.write_bytes(b"\xff\xd8orphan")

    view._prune_thumbnail_cache(identities)

    assert expected_paths[0].exists() is False
    assert expected_paths[1].exists() is True
    assert expected_paths[2].exists() is True
    assert orphan.exists() is False


def test_thumbnail_cache_obeys_total_byte_limit(monkeypatch, tmp_path):
    thumbnail_dir = tmp_path / "thumbnails"
    thumbnail_dir.mkdir()
    monkeypatch.setattr(clips_module, "THUMBNAIL_DIR", thumbnail_dir)
    monkeypatch.setattr(clips_module, "MAX_THUMBNAIL_CACHE_BYTES", 15)
    view = _make_view()
    identities = {}
    expected_paths = []
    for index in range(2):
        clip_path = tmp_path / f"clip-{index}.mkv"
        identity = (index + 1, 100 + index)
        identities[str(clip_path)] = identity
        thumbnail = view._thumbnail_path_for_identity(clip_path, *identity)
        thumbnail.write_bytes(b"\xff\xd8" + b"x" * 8)
        expected_paths.append(thumbnail)

    view._prune_thumbnail_cache(identities)

    assert expected_paths[0].exists() is False
    assert expected_paths[1].exists() is True


def test_trash_confirmation_dialog_explains_that_trashing_is_reversible(monkeypatch):
    class DialogStub:
        def __init__(self):
            self.responses = []

        def set_heading(self, value):
            self.heading = value

        def set_body(self, value):
            self.body = value

        def add_response(self, response_id, label):
            self.responses.append((response_id, label))

        def set_response_appearance(self, response_id, appearance):
            self.appearance = (response_id, appearance)

        def set_default_response(self, response_id):
            self.default_response = response_id

        def set_close_response(self, response_id):
            self.close_response = response_id

        def connect(self, *_args):
            pass

        def present(self):
            self.presented = True

    dialog = DialogStub()
    monkeypatch.setattr(
        clips_module.Adw.MessageDialog,
        "new",
        lambda *_args: dialog,
    )
    view = _make_view()
    view.get_root = lambda: object()

    view.on_delete_clip({"name": "Example.mkv", "path": "/clips/Example.mkv"}, object())

    assert dialog.heading == "Move clip to trash?"
    assert dialog.body == ("'Example.mkv' will be moved to the trash.\nThis can be reversed.")
    assert dialog.responses == [("cancel", "Cancel"), ("trash", "Move to trash")]
    assert dialog.default_response == "cancel"
    assert dialog.close_response == "cancel"
    assert dialog.presented is True


def test_shift_clicking_edit_opens_wipe_prompt_instead_of_the_editor():
    prompts = []
    opened = []
    button = types.SimpleNamespace(_shift_edit_armed=False)
    gesture = types.SimpleNamespace(
        get_current_event_state=lambda: clips_module.Gdk.ModifierType.SHIFT_MASK
    )
    view = _make_view()
    view._confirm_wipe_edits = lambda target, clip: prompts.append((target, clip))
    view.on_edit_clip = lambda target, clip: opened.append((target, clip))
    clip_data = {"name": "Example.mkv", "path": "/clips/Example.mkv"}

    view.on_edit_button_pressed(gesture, button, clip_data)
    view.on_edit_clip_clicked(button, clip_data)

    assert prompts == [(button, clip_data)]
    assert opened == []
    assert button._shift_edit_armed is False


def test_holding_shift_restylizes_all_visible_clip_action_buttons():
    class ButtonStub:
        def __init__(self):
            self.classes = set()
            self.icon_name = None
            self.tooltip = None

        def add_css_class(self, name):
            self.classes.add(name)

        def remove_css_class(self, name):
            self.classes.discard(name)

        def set_tooltip_text(self, text):
            self.tooltip = text

        def set_icon_name(self, name):
            self.icon_name = name

    edit_buttons = {ButtonStub(), ButtonStub()}
    delete_buttons = {ButtonStub(), ButtonStub()}
    view = _make_view()
    view._edit_buttons = edit_buttons
    view._delete_buttons = delete_buttons

    view.set_shift_edit_mode(True)

    assert all("destructive-action" in button.classes for button in edit_buttons)
    assert all(button.tooltip == "Edit clip from scratch" for button in edit_buttons)
    assert all(button.icon_name == "empty-trash-bin-symbolic" for button in delete_buttons)
    assert all(
        button.tooltip == "Permanently delete the clip" for button in delete_buttons
    )

    view.set_shift_edit_mode(False)

    assert all("destructive-action" not in button.classes for button in edit_buttons)
    assert all(button.tooltip == "Edit clip" for button in edit_buttons)
    assert all(button.icon_name == "xsi-user-trash-symbolic" for button in delete_buttons)
    assert all(button.tooltip == "Move clip to trash" for button in delete_buttons)


def test_clip_delete_button_uses_empty_bin_icon_and_permanent_delete_tooltip():
    assert clips_module.EMPTY_TRASH_BIN == "empty-trash-bin-symbolic"


def test_wipe_edits_confirmation_uses_destructive_sentence_case_text(monkeypatch):
    class DialogStub:
        def __init__(self):
            self.responses = []

        def set_heading(self, value):
            self.heading = value

        def set_body(self, value):
            self.body = value

        def add_response(self, response_id, label):
            self.responses.append((response_id, label))

        def set_response_appearance(self, response_id, appearance):
            self.appearance = (response_id, appearance)

        def set_default_response(self, response_id):
            self.default_response = response_id

        def set_close_response(self, response_id):
            self.close_response = response_id

        def connect(self, *args):
            self.connection = args

        def present(self):
            self.presented = True

    dialog = DialogStub()
    monkeypatch.setattr(clips_module.Adw.MessageDialog, "new", lambda *_args: dialog)
    view = _make_view()
    view.get_root = lambda: object()
    button = types.SimpleNamespace()
    clip_data = {"name": "Example.mkv", "path": "/clips/Example.mkv"}

    view._confirm_wipe_edits(button, clip_data)

    assert dialog.heading == "Wipe all current edits?"
    assert dialog.body == (
        "All cuts and audio changes for 'Example.mkv' will be permanently removed."
    )
    assert dialog.responses == [("cancel", "Cancel"), ("wipe", "Wipe edits")]
    assert dialog.appearance == (
        "wipe",
        clips_module.Adw.ResponseAppearance.DESTRUCTIVE,
    )
    assert dialog.default_response == "cancel"
    assert dialog.close_response == "cancel"
    assert dialog.connection == (
        "response",
        view._on_wipe_edits_confirmed,
        button,
        clip_data,
    )
    assert dialog.presented is True


def test_confirming_wipe_deletes_saved_edits_before_opening_editor(monkeypatch):
    import editor_drafts

    calls = []
    monkeypatch.setattr(
        editor_drafts,
        "delete_editor_data",
        lambda path: calls.append(("wipe", path)) or True,
    )
    view = _make_view()
    view.on_edit_clip = lambda button, clip: calls.append(("open", button, clip))
    button = types.SimpleNamespace(_shift_edit_armed=True)
    clip_data = {"name": "Example.mkv", "path": "/clips/Example.mkv"}

    view._on_wipe_edits_confirmed(None, "wipe", button, clip_data)

    assert calls == [
        ("wipe", "/clips/Example.mkv"),
        ("open", button, clip_data),
    ]
    assert button._shift_edit_armed is False


def test_cancelling_wipe_keeps_saved_edits_and_does_not_open_editor(monkeypatch):
    import editor_drafts

    calls = []
    monkeypatch.setattr(editor_drafts, "delete_editor_data", calls.append)
    view = _make_view()
    view.on_edit_clip = lambda *_args: calls.append("open")
    button = types.SimpleNamespace(_shift_edit_armed=True)

    view._on_wipe_edits_confirmed(None, "cancel", button, {"path": "/clip.mkv"})

    assert calls == []
    assert button._shift_edit_armed is False


def test_trash_confirmation_removes_row_and_starts_worker(monkeypatch, tmp_path):
    ThreadStub.created = []
    monkeypatch.setattr(clips_module.threading, "Thread", ThreadStub)

    clip_path = tmp_path / "clip.mkv"
    row = object()
    view = _make_view()
    view.clips_data = [{"path": clip_path}, {"path": tmp_path / "other.mkv"}]

    view.on_delete_confirmed(None, "trash", {"path": clip_path}, row)

    assert view.clips_list.removed == [row]
    assert view.clips_data == [{"path": tmp_path / "other.mkv"}]
    assert clip_path in view._locally_deleted_paths
    assert len(ThreadStub.created) == 1
    assert ThreadStub.created[0].args == (clip_path, False)
    assert ThreadStub.created[0].daemon is True
    assert ThreadStub.created[0].started is True
    assert view.refresh_called is False


def test_delete_worker_moves_clip_to_trash(monkeypatch, tmp_path):
    clip_path = tmp_path / "clip.mkv"
    clip_path.write_bytes(b"clip")
    trashed = []

    class TrashFileStub:
        def trash(self, _cancellable):
            trashed.append(clip_path)
            clip_path.unlink()
            return True

    monkeypatch.setattr(
        clips_module.Gio,
        "File",
        types.SimpleNamespace(new_for_path=lambda path: TrashFileStub()),
        raising=False,
    )
    view = _make_view()
    toasts = []
    view._show_toast = toasts.append

    view._delete_clip_file_worker(clip_path)

    assert trashed == [clip_path]
    assert clip_path.exists() is False
    assert toasts == ["Clip moved to trash"]


def test_permanent_delete_worker_unlinks_clip_without_using_trash(monkeypatch, tmp_path):
    clip_path = tmp_path / "clip.mkv"
    clip_path.write_bytes(b"clip")
    monkeypatch.setattr(
        clips_module.Gio,
        "File",
        types.SimpleNamespace(
            new_for_path=lambda _path: (_ for _ in ()).throw(
                AssertionError("Trash was used for permanent deletion")
            )
        ),
        raising=False,
    )
    view = _make_view()
    toasts = []
    view._show_toast = toasts.append

    view._delete_clip_file_worker(clip_path, permanently=True)

    assert clip_path.exists() is False
    assert toasts == ["Clip deleted"]


def test_successful_clip_disposal_removes_its_saved_editor_data(monkeypatch, tmp_path):
    import editor_drafts

    clip_path = tmp_path / "clip.mkv"
    removed = []
    monkeypatch.setattr(editor_drafts, "delete_editor_data", removed.append)
    view = _make_view()
    view._show_toast = lambda _message: None
    view._schedule_forget_locally_deleted_path = lambda _path: None

    view._on_delete_clip_finished(clip_path, None)

    assert removed == [clip_path]


def test_failed_clip_disposal_keeps_its_saved_editor_data(monkeypatch, tmp_path):
    import editor_drafts

    clip_path = tmp_path / "clip.mkv"
    removed = []
    monkeypatch.setattr(editor_drafts, "delete_editor_data", removed.append)
    view = _make_view()
    view._show_toast = lambda _message: None

    view._on_delete_clip_finished(clip_path, OSError("busy"))

    assert removed == []


def test_shift_delete_bypasses_confirmation(monkeypatch, tmp_path):
    ThreadStub.created = []
    monkeypatch.setattr(clips_module.threading, "Thread", ThreadStub)

    clip_path = tmp_path / "clip.mkv"
    row = object()
    button = types.SimpleNamespace(_shift_delete_armed=False)
    view = _make_view()
    view.clips_data = [{"path": clip_path}]

    confirmations = []
    view.on_delete_clip = lambda clip_data, row_obj: confirmations.append((clip_data, row_obj))
    gesture = types.SimpleNamespace(
        get_current_event_state=lambda: clips_module.Gdk.ModifierType.SHIFT_MASK
    )

    view.on_delete_button_pressed(gesture, button, {"path": clip_path}, row)
    view.on_delete_clip_clicked(button, {"path": clip_path}, row)

    assert confirmations == []
    assert view.clips_list.removed == [row]
    assert view.clips_data == []
    assert clip_path in view._locally_deleted_paths
    assert len(ThreadStub.created) == 1
    assert ThreadStub.created[0].args == (clip_path, True)
    assert ThreadStub.created[0].started is True
    assert button._shift_delete_armed is False


def test_local_delete_monitor_event_is_ignored(tmp_path):
    clip_path = tmp_path / "clip.mkv"
    view = _make_view()
    view._locally_deleted_paths.add(clip_path)

    assert view._monitor_event_mentions_video(GioFileStub(clip_path), None) is True
    assert view._monitor_event_mentions_locally_deleted_path(GioFileStub(clip_path), None) is True


def test_visible_clips_filters_by_name_case_insensitively(tmp_path):
    view = _make_view()
    view.clips_data = [
        {"path": tmp_path / "alpha.mkv", "name": "Alpha Moment.mkv", "mtime": 30},
        {"path": tmp_path / "beta.mkv", "name": "Beta Round.mkv", "mtime": 20},
        {"path": tmp_path / "final.mkv", "name": "final ALPHA.mkv", "mtime": 10},
    ]
    view.search_query = "alpha"

    assert [clip["name"] for clip in view._visible_clips()] == [
        "Alpha Moment.mkv",
        "final ALPHA.mkv",
    ]


def test_visible_clips_sorts_by_requested_order(tmp_path):
    view = _make_view()
    view.clips_data = [
        {"path": tmp_path / "zeta.mkv", "name": "Zeta.mkv", "mtime": 20},
        {"path": tmp_path / "alpha.mkv", "name": "alpha.mkv", "mtime": 30},
        {"path": tmp_path / "middle.mkv", "name": "Middle.mkv", "mtime": 10},
    ]

    assert [clip["name"] for clip in view._visible_clips()] == [
        "alpha.mkv",
        "Zeta.mkv",
        "Middle.mkv",
    ]

    view.sort_mode = ClipsView.SORT_OLDEST
    assert [clip["name"] for clip in view._visible_clips()] == [
        "Middle.mkv",
        "Zeta.mkv",
        "alpha.mkv",
    ]

    view.sort_mode = ClipsView.SORT_NAME
    assert [clip["name"] for clip in view._visible_clips()] == [
        "alpha.mkv",
        "Middle.mkv",
        "Zeta.mkv",
    ]


def test_game_data_for_clip_uses_current_whitelist_entry(tmp_path):
    icon_path = tmp_path / "icon.png"
    icon_path.write_bytes(b"png")
    clip_path = tmp_path / "Counter-Strike_2_2026-07-02_12-30-00.mkv"
    clip_path.write_bytes(b"clip")
    stat = clip_path.stat()

    view = _make_view()
    view.config = ConfigStub(
        {
            "whitelist": [
                {
                    "name": "Counter-Strike 2",
                    "icon_path": str(icon_path),
                    "install_path": str(tmp_path / "missing"),
                }
            ],
            "clip_game_metadata": {"clips": {}},
        }
    )

    game_data = view._game_data_for_clip(
        clip_path, stat, {}, view._whitelist_games_by_filename_token()
    )

    assert game_data == {
        "name": "Counter-Strike 2",
        "icon_path": str(icon_path),
        "install_path": str(tmp_path / "missing"),
    }


def test_game_data_for_clip_uses_cached_deleted_game(tmp_path):
    icon_path = tmp_path / "cs2.png"
    icon_path.write_bytes(b"png")
    clip_path = tmp_path / "Counter-Strike_2_2026-07-02_12-30-00.mkv"
    clip_path.write_bytes(b"clip")
    stat = clip_path.stat()
    cached = {
        str(clip_path): {
            "path": str(clip_path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "game": {"name": "Counter-Strike 2", "icon_path": str(icon_path)},
        }
    }

    view = _make_view()

    assert view._game_data_for_clip(clip_path, stat, cached, {}) == {
        "name": "Counter-Strike 2",
        "icon_path": str(icon_path),
    }


def test_game_data_for_clip_refreshes_cached_artwork_from_whitelist(tmp_path, monkeypatch):
    icon_path = tmp_path / "steam-icon.jpg"
    icon_path.write_bytes(b"jpg")
    clip_path = tmp_path / "THE_FINALS_2026-09-02_19-54-21.mkv"
    clip_path.write_bytes(b"clip")
    stat = clip_path.stat()
    cached = {
        str(clip_path): {
            "path": str(clip_path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "game": {
                "name": "THE FINALS",
                "install_path": "/old/location",
                "appid": "2073850",
            },
        }
    }
    whitelist = {
        "the_finals": {
            "name": "THE FINALS",
            "install_path": "/current/location",
            "appid": "2073850",
            "icon_path": str(icon_path),
        }
    }
    view = _make_view()
    monkeypatch.setattr(clips_module.steam, "get_game_icon_path", lambda _appid: "")

    assert view._game_data_for_clip(clip_path, stat, cached, whitelist) == {
        "name": "THE FINALS",
        "install_path": "/current/location",
        "appid": "2073850",
        "icon_path": str(icon_path),
    }


def test_compact_game_data_uses_steam_artwork_for_an_appid(tmp_path, monkeypatch):
    icon_path = tmp_path / "steam-icon.jpg"
    icon_path.write_bytes(b"jpg")
    monkeypatch.setattr(
        clips_module.steam,
        "get_game_icon_path",
        lambda appid: str(icon_path) if appid == "2073850" else "",
    )
    view = _make_view()

    assert view._compact_game_data({"name": "THE FINALS", "appid": "2073850"}) == {
        "name": "THE FINALS",
        "appid": "2073850",
        "icon_path": str(icon_path),
    }


def test_game_data_for_clip_handles_old_clips_without_game_data(tmp_path):
    clip_path = tmp_path / "2026-07-02_12-30-00.mkv"
    clip_path.write_bytes(b"clip")
    view = _make_view()

    assert view._game_data_for_clip(clip_path, clip_path.stat(), {}, {}) is None


def test_game_data_for_clip_falls_back_to_filename_for_removed_whitelist_game(tmp_path):
    clip_path = tmp_path / "Deep_Rock_Galactic_2026-07-02_12-30-00.mkv"
    clip_path.write_bytes(b"clip")
    view = _make_view()

    assert view._game_data_for_clip(clip_path, clip_path.stat(), {}, {}) == {
        "name": "Deep Rock Galactic"
    }


def test_clip_game_metadata_prunes_missing_files_and_caps_size(tmp_path):
    view = _make_view()
    metadata = {}
    for index in range(clips_module.MAX_CLIP_GAME_METADATA_ENTRIES + 3):
        clip_path = tmp_path / f"clip-{index}.mkv"
        clip_path.write_bytes(b"clip")
        metadata[str(clip_path)] = {
            "path": str(clip_path),
            "mtime_ns": index,
            "size": 4,
            "game": {"name": f"Game {index}"},
        }
    metadata[str(tmp_path / "missing.mkv")] = {
        "path": str(tmp_path / "missing.mkv"),
        "mtime_ns": 9999,
        "size": 4,
        "game": {"name": "Missing"},
    }

    pruned = view._pruned_clip_game_metadata(metadata)

    assert len(pruned) == clips_module.MAX_CLIP_GAME_METADATA_ENTRIES
    assert str(tmp_path / "missing.mkv") not in pruned
    assert str(tmp_path / "clip-0.mkv") not in pruned
    assert str(tmp_path / "clip-502.mkv") in pruned
