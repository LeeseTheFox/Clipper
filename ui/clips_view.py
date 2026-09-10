"""
Clips view - shows recent recorded clips
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import gi
from i18n import _, ngettext

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

try:
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland
except (ImportError, ValueError):
    GdkWayland = None

import steam
from config import ClipperConfig
from focus_helpers import dropdown_active_id, new_id_dropdown
from game_icons import cached_icon_path_for_game, submit_icon_resolution
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from icon_names import (
    CLAPPERBOARD_EDIT,
    CLIPS,
    EMPTY_TRASH_BIN,
    FOLDER_OPEN,
    GAME,
    PLAY,
    SEARCH,
    STEAM,
    TRASH,
)
from media_tools import clean_external_tool_env
from text_helpers import ELLIPSIZE_END, configure_single_line_ellipsis

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".flv", ".ts"}


def _default_cache_dir(env=None):
    """Return Clipper's XDG-aware cache directory."""
    environment = env if env is not None else os.environ
    cache_home = environment.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "clipper"
    return Path.home() / ".cache" / "clipper"


CLIPPER_CACHE_DIR = _default_cache_dir()
THUMBNAIL_DIR = CLIPPER_CACHE_DIR / "thumbnails"
MEDIA_METADATA_CACHE_FILE = CLIPPER_CACHE_DIR / "media-metadata.json"
MEDIA_METADATA_CACHE_VERSION = 1
MAX_MEDIA_METADATA_ENTRIES = 1_000
MAX_THUMBNAIL_CACHE_ENTRIES = 1_000
MAX_THUMBNAIL_CACHE_BYTES = 64 * 1024 * 1024
THUMBNAIL_WIDTH = 144
THUMBNAIL_HEIGHT = 81
THUMBNAIL_CACHE_VERSION = 3
CLIP_ROW_CONTENT_HEIGHT = THUMBNAIL_HEIGHT + 24
MEDIA_ENRICHMENT_BATCH_SIZE = 8
MEDIA_CACHE_SAVE_DELAY_MS = 750
CLIP_GAME_METADATA_CONFIG_KEY = "clip_game_metadata"
MAX_CLIP_GAME_METADATA_ENTRIES = 500
MAX_CLIP_GAME_NAME_FILENAME_CHARS = 95
CLIP_GAME_FILENAME_RE = re.compile(
    r"^(?P<game>.+)_(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<time>\d{2}-\d{2}-\d{2})$"
)
_THUMBNAIL_CSS_INSTALLED = False
_MEDIA_CACHE_LOCK = threading.Lock()


def _install_thumbnail_css():
    """Install local CSS for fixed, rounded clip thumbnails and game icons."""
    global _THUMBNAIL_CSS_INSTALLED
    if _THUMBNAIL_CSS_INSTALLED:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"""
        .clipper-thumbnail {
            min-width: 144px;
            min-height: 81px;
            border-radius: 8px;
            background-color: alpha(currentColor, 0.08);
            border: 1px solid alpha(currentColor, 0.14);
        }

        .clipper-thumbnail-image {
            border-radius: 8px;
        }

        .clipper-thumbnail-play {
            min-width: 42px;
            min-height: 42px;
            padding: 0;
            border-radius: 9999px;
            color: white;
            background-color: alpha(black, 0.68);
            box-shadow: 0 2px 8px alpha(black, 0.35);
        }

        .clipper-thumbnail-play:hover {
            background-color: alpha(black, 0.82);
        }

        .clipper-thumbnail-play:active {
            background-color: alpha(black, 0.92);
        }

        .clipper-game-artwork-icon {
            border-radius: 3px;
        }

        /* Libadwaita's boxed-list rules target GtkListBox's `list` node but
         * not GtkListView's `listview` node. Mirror those card rules so moving
         * to recycled rows does not alter the clip browser's appearance. */
        listview.clipper-boxed-list {
            background-color: var(--card-bg-color);
            color: var(--card-fg-color);
            border-radius: 12px;
            box-shadow: 0 0 0 1px RGB(0 0 6 / 3%),
                        0 1px 3px 1px RGB(0 0 6 / 7%),
                        0 2px 6px 2px RGB(0 0 6 / 3%);
        }

        listview.clipper-boxed-list > row {
            border-bottom: 1px solid var(--card-shade-color);
        }

        listview.clipper-boxed-list > row:first-child {
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
        }

        listview.clipper-boxed-list > row:last-child {
            border-bottom-left-radius: 12px;
            border-bottom-right-radius: 12px;
            border-bottom-width: 0;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _THUMBNAIL_CSS_INSTALLED = True


def _clean_external_tool_env():
    """Backward-compatible alias for the shared media environment helper."""
    return clean_external_tool_env()


class ClipsView(Gtk.Box):
    """View displaying recent clips with thumbnail, duration, and actions"""

    SORT_NEWEST = "newest"
    SORT_OLDEST = "oldest"
    SORT_NAME = "name"

    def __init__(
        self,
        config=None,
        edit_clip_callback=None,
        clips_rendered_callback=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(12)
        self.set_margin_bottom(12)

        self.config = config or ClipperConfig()
        self.edit_clip_callback = edit_clip_callback
        self.clips_rendered_callback = clips_rendered_callback
        self.clips_data = []
        self.search_query = ""
        self.sort_mode = self.SORT_NEWEST
        self._loading = False
        self._folder_monitor = None
        self._refresh_timeout_id = None
        self._idle_load_id = None
        self._media_load_generation = 0
        self._media_enrichment_start_id = None
        self._media_cache_save_id = None
        self._media_enrichment_running = False
        self._media_pending_jobs = {}
        self._media_inflight_paths = set()
        self._media_metadata = {}
        self._clip_identities = {}
        self._clips_by_path = {}
        self._media_widgets = {}
        self._visible_clip_data = []
        self._clip_string_model = None
        self._clip_selection_model = None
        self._bound_clip_paths = set()
        self._clips_rendered_pending = False
        self._watched_output_folder = None
        self._locally_deleted_paths = set()
        self._local_delete_forget_ids = {}
        self._edit_buttons = set()
        self._delete_buttons = set()
        self._shift_edit_mode = False
        self._player_process = None
        self._player_launch_generation = 0
        self._exported_player_parents = {}

        self.setup_ui()
        self._watch_output_folder()
        # Start loading clips asynchronously
        self._start_loading()

    def setup_ui(self):
        """Build the clips view UI"""
        _install_thumbnail_css()

        # View controls
        controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls_box.set_margin_start(6)
        controls_box.set_margin_end(6)
        controls_box.set_margin_bottom(12)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_width_chars(12)
        self.search_entry.set_placeholder_text(_("Search clips by name"))
        self.search_entry.connect("search-changed", self.on_search_changed)

        # Add Escape key handler to unfocus search entry
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_search_key_pressed)
        self.search_entry.add_controller(key_controller)

        controls_box.append(self.search_entry)

        self.trailing_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.trailing_controls.set_valign(Gtk.Align.CENTER)

        sort_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sort_box.set_valign(Gtk.Align.CENTER)

        sort_options = (
            (_("Newest"), self.SORT_NEWEST),
            (_("Oldest"), self.SORT_OLDEST),
            (_("A-Z"), self.SORT_NAME),
        )
        self.sort_dropdown = new_id_dropdown(sort_options, self.SORT_NEWEST)
        self.sort_dropdown.set_tooltip_text(_("Sort clips"))
        self.sort_dropdown.connect("notify::selected", self.on_sort_changed)
        sort_box.append(self.sort_dropdown)
        self.trailing_controls.append(sort_box)

        folder_button = Gtk.Button.new_from_icon_name(FOLDER_OPEN)
        folder_button.set_tooltip_text(_("Open clips folder"))
        folder_button.connect("clicked", self.on_open_folder)
        self.trailing_controls.append(folder_button)
        controls_box.append(self.trailing_controls)

        self.append(controls_box)

        # Scrolled window for clips list
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Gtk.ListView recycles row widgets outside the viewport. The model may
        # contain thousands of lightweight paths without constructing thousands
        # of thumbnail/button widget trees.
        self._clip_factory = Gtk.SignalListItemFactory.new()
        self._clip_factory.connect("setup", self._on_clip_item_setup)
        self._clip_factory.connect("bind", self._on_clip_item_bind)
        self._clip_factory.connect("unbind", self._on_clip_item_unbind)
        self._clip_string_model = Gtk.StringList.new([])
        self._clip_selection_model = Gtk.NoSelection.new(self._clip_string_model)
        self.clips_list = Gtk.ListView.new(self._clip_selection_model, self._clip_factory)
        self.clips_list.add_css_class("boxed-list")
        self.clips_list.add_css_class("clipper-boxed-list")
        self.clips_list.set_margin_start(6)
        self.clips_list.set_margin_end(6)
        self.clips_list.set_margin_top(6)
        self.clips_list.set_margin_bottom(6)
        # Disable focus to prevent scroll jumping when rows are deleted.
        self.clips_list.set_can_focus(False)
        self.scrolled.set_child(self.clips_list)

        # Empty state placeholder
        self.empty_state = self.create_empty_state()
        self.no_results_state = self.create_no_results_state()

        # Loading state
        self.loading_state = self.create_loading_state()

        self.append(self.scrolled)

    def _on_search_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events in the search entry."""
        if keyval == Gdk.KEY_Escape:
            # Clear focus from search entry on Escape
            root = self.search_entry.get_root()
            if root is not None and hasattr(root, "set_focus"):
                root.set_focus(None)
            return True  # Event handled
        return False  # Let other keys pass through

    def load_clips(self):
        """Display clips without waiting for external media tools."""
        output_folder = Path(self.config["output_folder"])
        self._idle_load_id = None
        self._media_load_generation += 1

        self.clips_data = []

        # Check if output folder exists
        if not output_folder.exists():
            output_folder.mkdir(parents=True, exist_ok=True)

        # Scan for supported video files
        try:
            video_files = [
                path
                for path in output_folder.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
            ]
        except Exception as e:
            print(f"Error scanning clips folder: {e}")
            video_files = []

        # Extract clip metadata
        game_metadata = self._load_clip_game_metadata_cache()
        media_metadata = self._load_media_metadata_cache()
        self._media_metadata = media_metadata
        self._clip_identities = {}
        self._clips_by_path = {}
        self._media_pending_jobs = {}
        self._media_inflight_paths = set()
        whitelist_games = self._whitelist_games_by_filename_token()
        refreshed_game_metadata = {}
        for clip_path in video_files:
            try:
                stat = clip_path.stat()
                self._clip_identities[str(clip_path)] = (
                    stat.st_mtime_ns,
                    stat.st_size,
                )
                game_data = self._game_data_for_clip(
                    clip_path, stat, game_metadata, whitelist_games
                )
                clip_info = {
                    "path": clip_path,
                    "name": clip_path.name,
                    "mtime": stat.st_mtime,
                    "mtime_ns": stat.st_mtime_ns,
                    "size_bytes": stat.st_size,
                    "time": self.format_time_ago(stat.st_mtime),
                    "size": self.format_file_size(stat.st_size),
                    "duration": self._cached_video_duration(clip_path, stat, media_metadata),
                    # Thumbnail filesystem checks are deferred until a clip is
                    # in a rendered row. Large libraries should not stat every
                    # cache entry during startup.
                    "thumbnail": None,
                }
                if game_data:
                    clip_info["game"] = game_data
                    refreshed_game_metadata[str(clip_path)] = self._clip_game_metadata_entry(
                        clip_path, stat, game_data
                    )
                self.clips_data.append(clip_info)
                self._clips_by_path[str(clip_path)] = clip_info
            except Exception as e:
                print(f"Error reading clip {clip_path}: {e}")

        self._save_clip_game_metadata_cache(refreshed_game_metadata)
        self._render_clips()

        self._loading = False
        self._schedule_media_cache_save()
        return False  # Don't repeat idle callback

    def on_search_changed(self, search_entry):
        """Update visible clips when the user changes the search text."""
        self.search_query = search_entry.get_text().strip()
        self._render_clips()

    def on_sort_changed(self, sort_dropdown, *_unused):
        """Update visible clips when the selected sort changes."""
        self.sort_mode = dropdown_active_id(sort_dropdown, self.SORT_NEWEST)
        self._render_clips()

    def _render_clips(self):
        """Replace the virtualized model after filtering and sorting."""
        self._clear_clip_rows()
        self._media_widgets = {}
        self._visible_clip_data = []
        self._media_pending_jobs = {}
        self._clips_rendered_pending = False

        if not self.clips_data:
            self.scrolled.set_child(self.empty_state)
            self._notify_clips_rendered()
            return

        self._visible_clip_data = self._visible_clips()
        if not self._visible_clip_data:
            self.scrolled.set_child(self.no_results_state)
            self._notify_clips_rendered()
            return

        self.scrolled.set_child(self.clips_list)
        self._clips_rendered_pending = True
        self._replace_clip_model(self._visible_clip_data)

    def _replace_clip_model(self, clips):
        paths = [str(clip["path"]) for clip in clips]
        self._clip_string_model = Gtk.StringList.new(paths)
        self._clip_selection_model = Gtk.NoSelection.new(self._clip_string_model)
        self.clips_list.set_model(self._clip_selection_model)

    def _on_clip_item_setup(self, _factory, list_item):
        list_item.set_child(self._new_clip_item_placeholder())

    def _new_clip_item_placeholder(self):
        placeholder = Gtk.Box()
        # Give ListView the real row height before binding. Without this hint it
        # estimates from an empty child and needlessly creates hundreds of rows.
        placeholder.set_size_request(-1, CLIP_ROW_CONTENT_HEIGHT)
        return placeholder

    def _on_clip_item_bind(self, _factory, list_item):
        item = list_item.get_item()
        path = item.get_string() if item is not None else ""
        clip = self._clips_by_path.get(path)
        if clip is None:
            return

        previous_path = getattr(list_item, "_clipper_path", "")
        if previous_path and previous_path != path:
            self._bound_clip_paths.discard(previous_path)
            self._media_widgets.pop(previous_path, None)
        self._prepare_clip_media(clip)
        list_item.set_child(self.create_clip_row(clip))
        list_item._clipper_path = path
        self._bound_clip_paths.add(path)
        self._queue_media_for_clips((clip,))

        if self._clips_rendered_pending:
            self._clips_rendered_pending = False
            self._notify_clips_rendered()

    def _on_clip_item_unbind(self, _factory, list_item):
        row = list_item.get_child()
        edit_button = getattr(row, "_clipper_edit_button", None)
        if edit_button is not None:
            self._edit_buttons.discard(edit_button)
        delete_button = getattr(row, "_clipper_delete_button", None)
        if delete_button is not None:
            self._delete_buttons.discard(delete_button)
        path = getattr(list_item, "_clipper_path", "")
        if path:
            self._bound_clip_paths.discard(path)
            self._media_widgets.pop(path, None)
            if path not in self._media_inflight_paths:
                self._media_pending_jobs.pop(path, None)
        list_item.set_child(self._new_clip_item_placeholder())
        list_item._clipper_path = ""

    def _notify_clips_rendered(self):
        """Notify the application after the complete clip-list widget tree exists."""
        callback = getattr(self, "clips_rendered_callback", None)
        if callback is not None:
            callback()

    def _clear_clip_rows(self):
        """Detach every recycled row by replacing the list model."""
        self._bound_clip_paths.clear()
        self._edit_buttons.clear()
        self._delete_buttons.clear()
        self._replace_clip_model(())

    def set_shift_edit_mode(self, active):
        """Reflect whether Edit buttons currently mean starting from scratch."""
        self._shift_edit_mode = bool(active)
        for button in tuple(self._edit_buttons):
            self._style_edit_button(button)
        for button in tuple(self._delete_buttons):
            self._style_delete_button(button)

    def _style_edit_button(self, button):
        if self._shift_edit_mode:
            button.add_css_class("destructive-action")
            button.set_tooltip_text(_("Edit clip from scratch"))
        else:
            button.remove_css_class("destructive-action")
            button.set_tooltip_text(_("Edit clip"))

    def _style_delete_button(self, button):
        if self._shift_edit_mode:
            button.set_icon_name(EMPTY_TRASH_BIN)
            button.set_tooltip_text(_("Permanently delete the clip"))
        else:
            button.set_icon_name(TRASH)
            button.set_tooltip_text(_("Move clip to trash"))

    def _visible_clips(self):
        """Return clips matching the active search query in the active sort order."""
        query = self.search_query.casefold()
        clips = [
            clip
            for clip in self.clips_data
            if not query or query in clip.get("name", "").casefold()
        ]

        if self.sort_mode == self.SORT_OLDEST:
            return sorted(
                clips,
                key=lambda clip: (clip.get("mtime", 0), self._clip_name_key(clip)),
            )

        if self.sort_mode == self.SORT_NAME:
            return sorted(
                clips,
                key=lambda clip: (self._clip_name_key(clip), -clip.get("mtime", 0)),
            )

        return sorted(
            clips,
            key=lambda clip: (-clip.get("mtime", 0), self._clip_name_key(clip)),
        )

    def _clip_name_key(self, clip):
        """Return a case-insensitive name key for sorting clips."""
        return clip.get("name", "").casefold()

    def _load_clip_game_metadata_cache(self):
        value = self.config.get(CLIP_GAME_METADATA_CONFIG_KEY, {})
        if not isinstance(value, dict):
            return {}

        clips = value.get("clips", value)
        if not isinstance(clips, dict):
            return {}

        return {
            str(path): entry
            for path, entry in clips.items()
            if isinstance(entry, dict) and isinstance(entry.get("game"), dict)
        }

    def _save_clip_game_metadata_cache(self, metadata):
        pruned = self._pruned_clip_game_metadata(metadata)
        current = self._load_clip_game_metadata_cache()
        if current == pruned:
            return
        self.config.set(CLIP_GAME_METADATA_CONFIG_KEY, {"clips": pruned})

    def _pruned_clip_game_metadata(self, metadata):
        entries = []
        for clip_path, entry in metadata.items():
            if not isinstance(entry, dict):
                continue
            game_data = entry.get("game")
            if not isinstance(game_data, dict) or not game_data.get("name"):
                continue
            if not Path(str(clip_path)).is_file():
                continue
            entries.append((str(clip_path), entry))

        entries.sort(
            key=lambda item: (
                int(item[1].get("mtime_ns", 0) or 0),
                str(item[0]),
            ),
            reverse=True,
        )
        return dict(entries[:MAX_CLIP_GAME_METADATA_ENTRIES])

    def _clip_game_metadata_entry(self, clip_path, stat, game_data):
        return {
            "path": str(clip_path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "game": self._compact_game_data(game_data),
        }

    def _game_data_for_clip(self, clip_path, stat, metadata, whitelist_games):
        token = self._game_token_from_clip_filename(clip_path)
        whitelist_game = whitelist_games.get(token.casefold()) if token else None

        cached = metadata.get(str(clip_path))
        if self._clip_game_metadata_entry_matches(cached, stat):
            game_data = self._compact_game_data(cached["game"])
            if whitelist_game and not cached_icon_path_for_game(game_data):
                # Preserve historical clip metadata, but let the current
                # whitelist fill artwork that was previously unavailable.
                game_data = self._compact_game_data({**game_data, **whitelist_game})
            return game_data

        if not token:
            return None

        if whitelist_game:
            return self._compact_game_data(whitelist_game)

        display_name = self._display_game_name_from_filename_token(token)
        return {"name": display_name} if display_name else None

    def _clip_game_metadata_entry_matches(self, entry, stat):
        if not isinstance(entry, dict) or not isinstance(entry.get("game"), dict):
            return False
        return (
            entry.get("mtime_ns") == stat.st_mtime_ns
            and entry.get("size") == stat.st_size
            and bool(entry["game"].get("name"))
        )

    def _whitelist_games_by_filename_token(self):
        games = {}
        for game_data in self.config.get("whitelist", []) or []:
            if not isinstance(game_data, dict):
                continue
            token = self._sanitize_clip_filename_part(game_data.get("name", ""))
            if token:
                games.setdefault(token.casefold(), game_data)
        return games

    def _game_token_from_clip_filename(self, clip_path):
        match = CLIP_GAME_FILENAME_RE.match(Path(clip_path).stem)
        if not match:
            return ""
        return match.group("game").strip()

    def _display_game_name_from_filename_token(self, token):
        return re.sub(r"_+", " ", token).strip()

    def _sanitize_clip_filename_part(self, value):
        output = []
        pending_separator = False
        for char in str(value or ""):
            if len(output) >= MAX_CLIP_GAME_NAME_FILENAME_CHARS:
                break
            if char.isalnum() or char in "-_.":
                output.append(char)
                pending_separator = False
            elif not pending_separator and output:
                output.append("_")
                pending_separator = True

        while output and output[-1] == "_":
            output.pop()
        return "".join(output)

    def _compact_game_data(self, game_data):
        if not isinstance(game_data, dict):
            return {}

        compact = {}
        for key in (
            "name",
            "executable_name",
            "executable_path",
            "path",
            "install_path",
            "appid",
        ):
            value = game_data.get(key)
            if isinstance(value, (str, int)) and str(value):
                compact[key] = str(value)

        icon_path_value = game_data.get("icon_path")
        if isinstance(icon_path_value, str) and icon_path_value:
            compact["icon_path"] = icon_path_value
        icon_path = cached_icon_path_for_game(compact)
        if not icon_path and compact.get("appid"):
            icon_path = steam.get_game_icon_path(compact["appid"])
        if icon_path:
            compact["icon_path"] = icon_path
        else:
            compact.pop("icon_path", None)
        return compact

    def _load_media_metadata_cache(self):
        """Load cached duration data used to avoid startup-time ffprobe calls."""
        try:
            data = json.loads(MEDIA_METADATA_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict) or data.get("version") != MEDIA_METADATA_CACHE_VERSION:
            return {}
        clips = data.get("clips")
        if not isinstance(clips, dict):
            return {}
        return {str(path): entry for path, entry in clips.items() if isinstance(entry, dict)}

    def _save_media_metadata_cache(self, metadata, identities=None):
        """Persist bounded media metadata and remove orphaned thumbnails."""
        identities = identities or {
            str(path): (entry.get("mtime_ns"), entry.get("size"))
            for path, entry in metadata.items()
            if isinstance(entry, dict)
        }
        clips = self._pruned_media_metadata(metadata, identities)
        payload = {
            "version": MEDIA_METADATA_CACHE_VERSION,
            "clips": clips,
        }
        with _MEDIA_CACHE_LOCK:
            try:
                current = json.loads(MEDIA_METADATA_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                current = None

            for stale in MEDIA_METADATA_CACHE_FILE.parent.glob("media-metadata-*.tmp"):
                try:
                    stale.unlink()
                except OSError:
                    pass

            if current != payload:
                try:
                    MEDIA_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=MEDIA_METADATA_CACHE_FILE.parent,
                        delete=False,
                        prefix="media-metadata-",
                        suffix=".tmp",
                    ) as temporary:
                        json.dump(payload, temporary, separators=(",", ":"))
                        temporary_path = Path(temporary.name)
                    temporary_path.replace(MEDIA_METADATA_CACHE_FILE)
                except OSError as error:
                    print(f"Could not save clip media cache: {error}")

            self._prune_thumbnail_cache(identities)

    def _pruned_media_metadata(self, metadata, identities):
        entries = []
        for path, entry in metadata.items():
            identity = identities.get(str(path))
            if not isinstance(entry, dict) or not identity:
                continue
            if entry.get("mtime_ns") != identity[0] or entry.get("size") != identity[1]:
                continue
            if not entry.get("duration") and not entry.get("thumbnail"):
                continue
            entries.append((str(path), entry))

        entries.sort(
            key=lambda item: (
                int(item[1].get("mtime_ns", 0) or 0),
                item[0],
            ),
            reverse=True,
        )
        return dict(entries[:MAX_MEDIA_METADATA_ENTRIES])

    def _prune_thumbnail_cache(self, identities):
        """Bound thumbnails by current clips, entry count, and total bytes."""
        try:
            cached_files = [
                path
                for path in THUMBNAIL_DIR.iterdir()
                if path.is_file() and path.suffix.casefold() == ".jpg"
            ]
        except OSError:
            return

        expected = []
        for path, identity in identities.items():
            if not identity:
                continue
            expected.append(
                (
                    int(identity[0]),
                    self._thumbnail_path_for_identity(Path(path), identity[0], identity[1]),
                )
            )
        expected.sort(key=lambda item: (item[0], str(item[1])), reverse=True)

        keep = set()
        total_bytes = 0
        for _mtime_ns, path in expected[:MAX_THUMBNAIL_CACHE_ENTRIES]:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total_bytes + size > MAX_THUMBNAIL_CACHE_BYTES:
                continue
            keep.add(path)
            total_bytes += size

        for path in cached_files:
            if path not in keep:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _cached_video_duration(self, video_path, stat, metadata):
        entry = metadata.get(str(video_path))
        if not isinstance(entry, dict):
            return None
        if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
            return None
        duration = entry.get("duration")
        return duration if isinstance(duration, str) and duration else None

    def _media_metadata_entry(self, stat, duration, thumbnail=None):
        entry = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "duration": duration,
        }
        if thumbnail:
            entry["thumbnail"] = Path(thumbnail).name
        return entry

    def _prepare_clip_media(self, clip):
        """Resolve a cached thumbnail only when its row is about to be shown."""
        if clip.get("thumbnail"):
            return
        mtime_ns = clip.get("mtime_ns")
        size = clip.get("size_bytes")
        if mtime_ns is None or size is None:
            return
        thumbnail = self._cached_video_thumbnail_for_identity(clip["path"], mtime_ns, size)
        if thumbnail:
            clip["thumbnail"] = thumbnail

    def _queue_media_for_clips(self, clips):
        """Queue media probes only for the currently materialized row batches."""
        for clip in clips:
            if clip.get("duration") and clip.get("thumbnail"):
                continue
            mtime_ns = clip.get("mtime_ns")
            size = clip.get("size_bytes")
            if mtime_ns is None or size is None:
                continue
            path = str(clip["path"])
            if path in self._media_inflight_paths:
                continue
            self._media_pending_jobs[path] = {
                "path": clip["path"],
                "mtime_ns": mtime_ns,
                "size": size,
                "duration": clip.get("duration"),
                "thumbnail": clip.get("thumbnail"),
            }
        self._schedule_media_enrichment_worker()

    def _schedule_media_enrichment_worker(self):
        if (
            not self._media_pending_jobs
            or self._media_enrichment_running
            or self._media_enrichment_start_id is not None
        ):
            return

        self._media_enrichment_start_id = GLib.timeout_add(150, self._start_media_enrichment_worker)

    def _start_media_enrichment_worker(self):
        self._media_enrichment_start_id = None
        if self._media_enrichment_running or not self._media_pending_jobs:
            return False

        paths = list(self._media_pending_jobs)[:MEDIA_ENRICHMENT_BATCH_SIZE]
        jobs = [self._media_pending_jobs.pop(path) for path in paths]
        generation = self._media_load_generation
        self._media_enrichment_running = True
        self._media_inflight_paths.update(paths)
        threading.Thread(
            target=self._enrich_media_worker,
            args=(generation, jobs),
            name="clipper-clip-media",
            daemon=True,
        ).start()
        return False

    def _enrich_media_worker(self, generation, jobs):
        """Probe uncached media properties away from GTK's main thread."""
        results = {}
        for job in jobs:
            clip_path = job["path"]
            try:
                stat = clip_path.stat()
                if stat.st_mtime_ns != job["mtime_ns"] or stat.st_size != job["size"]:
                    continue
                duration = job["duration"] or self.get_video_duration(clip_path)
                results[str(clip_path)] = {
                    "duration": duration,
                    "thumbnail": job["thumbnail"],
                    "mtime_ns": job["mtime_ns"],
                    "size": job["size"],
                }
            except OSError:
                continue

        GLib.idle_add(self._apply_media_enrichment, generation, results)

        # Thumbnail decoding is usually the slowest part. Generate and reveal
        # each missing image progressively after all durations are available.
        for job in jobs:
            if job["thumbnail"]:
                continue
            clip_path = job["path"]
            result = results.get(str(clip_path))
            if result is None:
                continue
            try:
                stat = clip_path.stat()
                if stat.st_mtime_ns != job["mtime_ns"] or stat.st_size != job["size"]:
                    continue
                thumbnail = self.get_video_thumbnail(clip_path, stat)
            except OSError:
                continue
            GLib.idle_add(
                self._apply_media_enrichment,
                generation,
                {
                    str(clip_path): {
                        "duration": result["duration"],
                        "thumbnail": thumbnail,
                        "mtime_ns": job["mtime_ns"],
                        "size": job["size"],
                    }
                },
            )
        GLib.idle_add(
            self._on_media_enrichment_batch_finished,
            generation,
            tuple(str(job["path"]) for job in jobs),
        )

    def _apply_media_enrichment(self, generation, results):
        """Apply background media results without rebuilding or scrolling the list."""
        if generation != self._media_load_generation:
            return False

        for path, result in results.items():
            if self._clip_identities.get(path) != (
                result["mtime_ns"],
                result["size"],
            ):
                continue
            clip = self._clips_by_path.get(path)
            if clip is None:
                continue
            if result.get("duration"):
                clip["duration"] = result["duration"]
            if result.get("thumbnail"):
                clip["thumbnail"] = result["thumbnail"]
            self._media_metadata[path] = {
                "mtime_ns": result["mtime_ns"],
                "size": result["size"],
                "duration": clip.get("duration"),
                "thumbnail": (Path(clip["thumbnail"]).name if clip.get("thumbnail") else None),
            }
            self._update_clip_media_widgets(clip)

        if len(self._media_metadata) > MAX_MEDIA_METADATA_ENTRIES:
            self._media_metadata = self._pruned_media_metadata(
                self._media_metadata, self._clip_identities
            )
        self._schedule_media_cache_save()
        return False

    def _on_media_enrichment_batch_finished(self, _generation, paths):
        self._media_inflight_paths.difference_update(paths)
        self._media_enrichment_running = False
        self._schedule_media_enrichment_worker()
        self._schedule_media_cache_save()
        return False

    def _schedule_media_cache_save(self):
        if self._media_cache_save_id is not None:
            GLib.source_remove(self._media_cache_save_id)
        self._media_cache_save_id = GLib.timeout_add(
            MEDIA_CACHE_SAVE_DELAY_MS, self._start_media_cache_save
        )

    def _start_media_cache_save(self):
        self._media_cache_save_id = None
        if (
            self._media_enrichment_running
            or self._media_pending_jobs
            or self._media_enrichment_start_id is not None
        ):
            self._media_cache_save_id = GLib.timeout_add(
                MEDIA_CACHE_SAVE_DELAY_MS, self._start_media_cache_save
            )
            return False
        metadata = dict(self._media_metadata)
        identities = dict(self._clip_identities)
        threading.Thread(
            target=self._save_media_metadata_cache,
            args=(metadata, identities),
            name="clipper-media-cache",
            daemon=True,
        ).start()
        return False

    def _thumbnail_path(self, video_path, stat):
        return self._thumbnail_path_for_identity(video_path, stat.st_mtime_ns, stat.st_size)

    def _thumbnail_path_for_identity(self, video_path, mtime_ns, size):
        cache_key = f"v{THUMBNAIL_CACHE_VERSION}:{video_path.resolve()}:{mtime_ns}:{size}"
        return THUMBNAIL_DIR / f"{sha256(cache_key.encode()).hexdigest()}.jpg"

    def _cached_video_thumbnail(self, video_path, stat):
        return self._cached_video_thumbnail_for_identity(video_path, stat.st_mtime_ns, stat.st_size)

    def _cached_video_thumbnail_for_identity(self, video_path, mtime_ns, size):
        thumbnail_path = self._thumbnail_path_for_identity(video_path, mtime_ns, size)
        return thumbnail_path if self._thumbnail_exists(thumbnail_path) else None

    def _set_clip_thumbnail_widget(self, container, thumbnail_path):
        # The placeholder image expands so Gtk.Box centers its icon, but that
        # expansion must stop at the fixed-size thumbnail container. Otherwise
        # Gtk.Overlay propagates it to the row and the placeholder thumbnail
        # shares horizontal space with the expanding info column until the real
        # thumbnail replaces it.
        container.set_hexpand(False)
        child = container.get_first_child()
        if child is not None:
            container.remove(child)

        if thumbnail_path:
            content = Gtk.Picture.new_for_filename(str(thumbnail_path))
            content.set_size_request(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
            content.set_can_shrink(True)
            content.set_halign(Gtk.Align.CENTER)
            content.set_valign(Gtk.Align.CENTER)
            content.add_css_class("clipper-thumbnail-image")
            if hasattr(content, "set_content_fit"):
                content.set_content_fit(Gtk.ContentFit.COVER)
        else:
            content = Gtk.Image.new_from_icon_name(CLIPS)
            content.set_pixel_size(32)
            # Gtk.Box otherwise gives this child only its 32 px natural width
            # and packs that allocation at the leading edge of the 144 px
            # thumbnail frame. Expanding the child centers the image inside
            # the complete placeholder area.
            content.set_hexpand(True)
            content.set_vexpand(True)
            content.set_halign(Gtk.Align.CENTER)
            content.set_valign(Gtk.Align.CENTER)
        container.append(content)
        container._clipper_has_thumbnail = bool(thumbnail_path)
        if getattr(container, "_clipper_play_hovered", False) and not thumbnail_path:
            content.set_opacity(0)

    @staticmethod
    def _set_thumbnail_play_hover(container, play_button, hovered):
        """Reveal the thumbnail action without overlapping its placeholder."""
        container._clipper_play_hovered = hovered
        content = container.get_first_child()
        if content is not None:
            content.set_opacity(
                0 if hovered and not getattr(container, "_clipper_has_thumbnail", False) else 1
            )
        play_button.set_visible(hovered)

    def _update_clip_media_widgets(self, clip_data):
        widgets = self._media_widgets.get(str(clip_data["path"]))
        if widgets is None:
            return
        thumbnail, duration_label = widgets
        self._set_clip_thumbnail_widget(thumbnail, clip_data.get("thumbnail"))
        duration = clip_data.get("duration") or ""
        duration_label.set_label(duration)
        duration_label.set_visible(bool(duration))

    def create_empty_state(self):
        """Create empty state widget"""
        status_page = Adw.StatusPage()
        status_page.set_icon_name(CLIPS)
        status_page.set_title(_("No clips yet"))
        status_page.set_description(
            _("Recorded clips will appear here.\nPress your save hotkey to capture a clip.")
        )
        return status_page

    def create_no_results_state(self):
        """Create empty search-results state widget."""
        status_page = Adw.StatusPage()
        status_page.set_icon_name(SEARCH)
        status_page.set_title(_("No matching clips"))
        status_page.set_description(_("No clip names match the current search."))
        return status_page

    def create_loading_state(self):
        """Create loading state widget"""
        status_page = Adw.StatusPage()
        status_page.set_title(_("Loading clips…"))

        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        status_page.set_child(spinner)

        return status_page

    def _start_loading(self):
        """Show loading state and start async clip loading"""
        if self._idle_load_id is not None:
            return
        self._loading = True
        self.scrolled.set_child(self.loading_state)
        # Schedule load_clips to run after the UI has been drawn
        self._idle_load_id = GLib.idle_add(self.load_clips)

    def refresh(self, show_loading=False):
        """Refresh clips from the current output folder."""
        if show_loading:
            self._start_loading()
            return

        if self._idle_load_id is None:
            self._idle_load_id = GLib.idle_add(self.load_clips)

    def cleanup(self):
        """Stop scheduled UI work and ignore any in-flight media worker."""
        self._media_load_generation += 1
        for attribute in (
            "_idle_load_id",
            "_refresh_timeout_id",
            "_media_enrichment_start_id",
            "_media_cache_save_id",
        ):
            source_id = getattr(self, attribute, None)
            if source_id is not None:
                GLib.source_remove(source_id)
                setattr(self, attribute, None)
        for source_id in self._local_delete_forget_ids.values():
            GLib.source_remove(source_id)
        self._local_delete_forget_ids.clear()
        if self._folder_monitor is not None:
            self._folder_monitor.cancel()
            self._folder_monitor = None

    def on_output_folder_changed(self, output_folder):
        """Refresh the list and file monitor after the output folder changes."""
        self.config.load()
        self._watch_output_folder(Path(output_folder))
        self.refresh(show_loading=True)

    def _watch_output_folder(self, output_folder=None):
        """Monitor the active output folder for external clip changes."""
        output_folder = Path(output_folder or self.config["output_folder"])
        if self._watched_output_folder == output_folder and self._folder_monitor is not None:
            return

        if self._folder_monitor is not None:
            self._folder_monitor.cancel()
            self._folder_monitor = None

        try:
            output_folder.mkdir(parents=True, exist_ok=True)
            self._watched_output_folder = output_folder
            folder = Gio.File.new_for_path(str(output_folder))
            self._folder_monitor = folder.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._folder_monitor.connect("changed", self._on_folder_changed)
        except Exception as e:
            print(f"Error watching clips folder: {e}")

    def _on_folder_changed(self, monitor, file, other_file, event_type):
        """Handle file monitor changes with a short debounce."""
        if event_type in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        ) and self._monitor_event_mentions_video(file, other_file):
            if self._monitor_event_mentions_locally_deleted_path(file, other_file):
                return
            self._schedule_refresh()

    def _monitor_event_mentions_video(self, file, other_file):
        """Return True when a monitor event refers to a supported clip file."""
        return any(
            path.suffix.lower() in VIDEO_EXTENSIONS
            for path in self._monitor_event_paths(file, other_file)
        )

    def _monitor_event_mentions_locally_deleted_path(self, file, other_file):
        """Return True for monitor events caused by a UI-initiated delete."""
        event_paths = self._monitor_event_paths(file, other_file)
        return any(path in self._locally_deleted_paths for path in event_paths)

    def _monitor_event_paths(self, file, other_file):
        """Return pathlib paths mentioned by a Gio file monitor event."""
        paths = []
        for gio_file in (file, other_file):
            if gio_file is None:
                continue
            path = gio_file.get_path()
            if path:
                paths.append(Path(path))
        return paths

    def _schedule_refresh(self):
        """Debounce refreshes from multi-event file monitor notifications."""
        if self._refresh_timeout_id is None:
            self._refresh_timeout_id = GLib.timeout_add(350, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self):
        self._refresh_timeout_id = None
        self.refresh()
        return False

    def format_time_ago(self, timestamp):
        """Format timestamp as human-readable relative time"""
        now = datetime.now().timestamp()
        diff = now - timestamp

        if diff < 60:
            return _("Just now")
        elif diff < 3600:
            minutes = int(diff / 60)
            return ngettext(
                "%(count)d minute ago", "%(count)d minutes ago", minutes
            ) % {"count": minutes}
        elif diff < 86400:
            hours = int(diff / 3600)
            return ngettext("%(count)d hour ago", "%(count)d hours ago", hours) % {
                "count": hours
            }
        elif diff < 172800:  # 2 days
            return _("Yesterday")
        else:
            days = int(diff / 86400)
            return ngettext("%(count)d day ago", "%(count)d days ago", days) % {
                "count": days
            }

    def format_file_size(self, size_bytes):
        """Format file size in human-readable format"""
        for unit in [_("B"), _("KB"), _("MB"), _("GB")]:
            if size_bytes < 1024.0:
                return _("%(size).1f %(unit)s") % {"size": size_bytes, "unit": unit}
            size_bytes /= 1024.0
        return _("%(size).1f TB") % {"size": size_bytes}

    def get_video_duration(self, video_path):
        """Get video duration using ffprobe, fallback to None if unavailable"""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                env=_clean_external_tool_env(),
                timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                duration_sec = float(data["format"]["duration"])
                minutes = int(duration_sec // 60)
                seconds = int(duration_sec % 60)
                return f"{minutes}:{seconds:02d}"
        except Exception:
            pass
        return None

    def get_video_thumbnail(self, video_path, stat):
        """Generate or return a cached thumbnail for a video clip."""
        try:
            THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
            thumbnail_path = self._thumbnail_path(video_path, stat)
            if self._thumbnail_exists(thumbnail_path):
                return thumbnail_path
            thumbnail_path.unlink(missing_ok=True)

            for timestamp in ("00:00:00.100", "00:00:00", "00:00:01"):
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(video_path),
                        "-ss",
                        timestamp,
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        "-an",
                        "-sn",
                        "-vf",
                        (
                            f"scale={THUMBNAIL_WIDTH}:{THUMBNAIL_HEIGHT}:"
                            "force_original_aspect_ratio=increase,"
                            f"crop={THUMBNAIL_WIDTH}:{THUMBNAIL_HEIGHT}"
                        ),
                        str(thumbnail_path),
                    ],
                    capture_output=True,
                    text=True,
                    env=_clean_external_tool_env(),
                    timeout=10,
                )
                if result.returncode == 0 and self._thumbnail_exists(thumbnail_path):
                    return thumbnail_path

            thumbnail_path.unlink(missing_ok=True)
            message = (result.stderr or result.stdout).strip().splitlines()
            if message:
                print(f"Could not generate thumbnail for {video_path.name}: {message[-1]}")
        except Exception as e:
            print(f"Could not generate thumbnail for {video_path.name}: {e}")
        return None

    def _thumbnail_exists(self, thumbnail_path):
        """Return True if ffmpeg produced a non-empty JPEG thumbnail."""
        try:
            if thumbnail_path.stat().st_size == 0:
                return False
            with thumbnail_path.open("rb") as file:
                return file.read(2) == b"\xff\xd8"
        except OSError:
            return False

    def create_clip_row(self, clip_data):
        """Create a row for a single clip"""
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)
        row_box.set_margin_top(12)
        row_box.set_margin_bottom(12)

        thumbnail = Gtk.Box()
        thumbnail.set_size_request(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        thumbnail.add_css_class("clipper-thumbnail")
        thumbnail.set_overflow(Gtk.Overflow.HIDDEN)
        thumbnail.set_valign(Gtk.Align.CENTER)
        thumbnail.set_halign(Gtk.Align.CENTER)
        self._set_clip_thumbnail_widget(thumbnail, clip_data.get("thumbnail"))

        thumbnail_overlay = Gtk.Overlay()
        thumbnail_overlay.set_child(thumbnail)

        play_button = Gtk.Button.new_from_icon_name(PLAY)
        play_button.set_halign(Gtk.Align.CENTER)
        play_button.set_valign(Gtk.Align.CENTER)
        play_button.set_tooltip_text(_("Play clip"))
        play_button.add_css_class("clipper-thumbnail-play")
        play_button.set_visible(False)
        play_button.connect("clicked", lambda _button: self.on_play_clip(clip_data))
        thumbnail_overlay.add_overlay(play_button)

        hover_controller = Gtk.EventControllerMotion()
        hover_controller.connect(
            "enter",
            lambda _controller, _x, _y: self._set_thumbnail_play_hover(
                thumbnail, play_button, True
            ),
        )
        hover_controller.connect(
            "leave",
            lambda _controller: self._set_thumbnail_play_hover(thumbnail, play_button, False),
        )
        thumbnail_overlay.add_controller(hover_controller)

        row_box.append(thumbnail_overlay)

        # Clip info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_valign(Gtk.Align.CENTER)
        info_box.set_hexpand(True)

        name_label = Gtk.Label(label=clip_data["name"])
        name_label.set_halign(Gtk.Align.START)
        name_label.set_hexpand(True)
        name_label.set_xalign(0)
        name_label.add_css_class("heading")
        configure_single_line_ellipsis(name_label, mode=ELLIPSIZE_END, max_width_chars=40)
        info_box.append(name_label)

        game_box = self._create_clip_game_box(clip_data.get("game"))
        if game_box is not None:
            info_box.append(game_box)

        # Time and size line
        meta_text = clip_data["time"]
        if clip_data.get("size"):
            meta_text += f" • {clip_data['size']}"

        time_label = Gtk.Label(label=meta_text)
        time_label.set_halign(Gtk.Align.START)
        time_label.set_hexpand(True)
        time_label.set_xalign(0)
        configure_single_line_ellipsis(time_label, mode=ELLIPSIZE_END)
        time_label.add_css_class("dim-label")
        time_label.add_css_class("caption")
        info_box.append(time_label)

        row_box.append(info_box)

        # Keep a hidden duration badge in place so background metadata can be
        # applied without rebuilding the row or disturbing the scroll position.
        duration = clip_data.get("duration") or ""
        duration_label = Gtk.Label(label=duration)
        duration_label.add_css_class("caption")
        duration_label.add_css_class("clipper-accent-chip")
        duration_label.set_valign(Gtk.Align.CENTER)
        duration_label.set_visible(bool(duration))
        row_box.append(duration_label)
        self._media_widgets[str(clip_data["path"])] = (thumbnail, duration_label)

        # Action buttons
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.set_valign(Gtk.Align.CENTER)

        edit_button = Gtk.Button.new_from_icon_name(CLAPPERBOARD_EDIT)
        edit_button.set_valign(Gtk.Align.CENTER)
        self._edit_buttons.add(edit_button)
        self._style_edit_button(edit_button)
        row_box._clipper_edit_button = edit_button
        edit_button._shift_edit_armed = False
        shift_edit_gesture = Gtk.GestureClick()
        shift_edit_gesture.connect(
            "pressed",
            lambda gesture, _n_press, _x, _y: self.on_edit_button_pressed(
                gesture, edit_button, clip_data
            ),
        )
        edit_button.add_controller(shift_edit_gesture)
        edit_button.connect(
            "clicked",
            lambda button: self.on_edit_clip_clicked(button, clip_data),
        )
        action_box.append(edit_button)

        reveal_button = Gtk.Button.new_from_icon_name(FOLDER_OPEN)
        reveal_button.set_valign(Gtk.Align.CENTER)
        reveal_button.set_tooltip_text(_("Reveal in file manager"))
        reveal_button.connect("clicked", lambda b: self.on_reveal_clip(clip_data))
        action_box.append(reveal_button)

        delete_button = Gtk.Button.new_from_icon_name(TRASH)
        delete_button.set_valign(Gtk.Align.CENTER)
        self._delete_buttons.add(delete_button)
        self._style_delete_button(delete_button)
        row_box._clipper_delete_button = delete_button
        delete_button.add_css_class("destructive-action")
        delete_button._shift_delete_armed = False
        shift_delete_gesture = Gtk.GestureClick()
        shift_delete_gesture.connect(
            "pressed",
            lambda gesture, _n_press, _x, _y: self.on_delete_button_pressed(
                gesture, delete_button, clip_data, row_box
            ),
        )
        delete_button.add_controller(shift_delete_gesture)
        delete_button.connect(
            "clicked",
            lambda b: self.on_delete_clip_clicked(b, clip_data, row_box),
        )
        action_box.append(delete_button)
        row_box.append(action_box)

        return row_box

    def on_edit_clip(self, button, clip_data):
        """Open a clip in the application-owned editor."""
        if self.edit_clip_callback is None:
            return
        button.set_sensitive(False)

        def completed():
            button.set_sensitive(True)

        try:
            self.edit_clip_callback(clip_data["path"], completed)
        except Exception:
            completed()
            raise

    def on_edit_button_pressed(self, gesture, button, clip_data):
        """Offer to wipe saved edits when the edit button is shift-clicked."""
        state = gesture.get_current_event_state()
        if state & Gdk.ModifierType.SHIFT_MASK:
            button._shift_edit_armed = True
            self._confirm_wipe_edits(button, clip_data)

    def on_edit_clip_clicked(self, button, clip_data):
        """Open the editor unless a shift-click already opened its wipe prompt."""
        if getattr(button, "_shift_edit_armed", False):
            button._shift_edit_armed = False
            return
        self.on_edit_clip(button, clip_data)

    def _confirm_wipe_edits(self, button, clip_data):
        from editor_wipe import present_wipe_edits_confirmation

        present_wipe_edits_confirmation(
            Adw,
            self.get_root(),
            clip_data["name"],
            self._on_wipe_edits_confirmed,
            button,
            clip_data,
        )

    def _on_wipe_edits_confirmed(self, _dialog, response, button, clip_data):
        button._shift_edit_armed = False
        if response != "wipe":
            return

        from editor_drafts import delete_editor_data

        if not delete_editor_data(clip_data["path"]):
            self._show_toast(_("Could not wipe saved edits"))
            return
        self.on_edit_clip(button, clip_data)

    def _create_clip_game_box(self, game_data):
        if not isinstance(game_data, dict):
            return None

        game_name = str(game_data.get("name") or "").strip()
        if not game_name:
            return None

        game_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        game_box.set_halign(Gtk.Align.START)
        game_box.set_valign(Gtk.Align.CENTER)
        game_box.add_css_class("clipper-metadata-chip")

        icon = self._new_game_icon(game_data, 18)
        game_box.append(icon)
        self._load_game_icon_async(game_data, icon, 18)

        game_label = Gtk.Label(label=game_name)
        game_label.set_halign(Gtk.Align.START)
        game_label.set_hexpand(True)
        game_label.set_xalign(0)
        configure_single_line_ellipsis(game_label, mode=ELLIPSIZE_END)
        game_label.add_css_class("dim-label")
        game_label.add_css_class("caption")
        game_box.append(game_label)
        return game_box

    def _new_game_icon(self, game_data, pixel_size):
        icon_path = cached_icon_path_for_game(game_data)
        if icon_path:
            icon = Gtk.Image.new_from_file(icon_path)
            icon.add_css_class("clipper-game-artwork-icon")
            icon.set_overflow(Gtk.Overflow.HIDDEN)
        else:
            icon_name = STEAM if "appid" in game_data else GAME
            icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(pixel_size)
        icon.set_valign(Gtk.Align.CENTER)
        return icon

    def _load_game_icon_async(self, game_data, icon, pixel_size):
        if cached_icon_path_for_game(game_data):
            return
        if not any(game_data.get(key) for key in ("executable_path", "path", "install_path")):
            return

        future = submit_icon_resolution(game_data)
        future.add_done_callback(
            lambda completed: GLib.idle_add(
                self._apply_resolved_game_icon, game_data, icon, pixel_size, completed
            )
        )

    def _apply_resolved_game_icon(self, game_data, icon, pixel_size, future):
        try:
            executable_path, icon_path = future.result()
        except Exception:  # noqa: BLE001
            return False

        if executable_path:
            game_data["executable_path"] = executable_path
        if not icon_path:
            return False

        game_data["icon_path"] = icon_path
        icon.set_from_file(icon_path)
        icon.add_css_class("clipper-game-artwork-icon")
        icon.set_overflow(Gtk.Overflow.HIDDEN)
        icon.set_pixel_size(pixel_size)
        return False

    def on_open_folder(self, button):
        """Open clips output folder"""
        output_folder = Path(self.config["output_folder"])
        output_folder.mkdir(parents=True, exist_ok=True)
        self._launch_path(output_folder, "Could not open folder")

    def on_play_clip(self, clip_data):
        """Open the selected clip in an isolated native GTK player."""
        self._player_launch_generation = getattr(self, "_player_launch_generation", 0) + 1
        generation = self._player_launch_generation
        previous = getattr(self, "_player_process", None)
        if previous is not None and previous.poll() is None:
            previous.terminate()

        if self._export_player_parent(clip_data, generation):
            return
        self._launch_clip_player(clip_data)

    def _export_player_parent(self, clip_data, generation):
        """Export the Clipper surface so Wayland can place the helper over it."""
        if GdkWayland is None:
            return False
        try:
            root = self.get_root()
            surface = root.get_surface() if root is not None else None
        except Exception:
            return False
        if surface is None:
            return False
        if not isinstance(surface, GdkWayland.WaylandToplevel):
            return False

        def parent_exported(exported_surface, parent_handle, _user_data):
            if generation != self._player_launch_generation:
                exported_surface.drop_exported_handle(parent_handle)
                return
            self._launch_clip_player(
                clip_data,
                parent_handle=parent_handle,
                parent_surface=exported_surface,
            )

        try:
            return bool(surface.export_handle(parent_exported, None))
        except Exception:
            return False

    def _launch_clip_player(self, clip_data, *, parent_handle=None, parent_surface=None):
        """Start the isolated helper, optionally related to an exported parent."""
        player_script = Path(__file__).with_name("video_player_window.py")
        args = [
            sys.executable,
            str(player_script),
            str(clip_data["path"]),
            "--title",
            str(clip_data.get("name") or Path(clip_data["path"]).name),
            "--parent-pid",
            str(os.getpid()),
        ]
        if parent_handle:
            args.extend(("--parent-handle", parent_handle))
        try:
            self._player_process = subprocess.Popen(args, close_fds=True)
        except Exception as error:
            self._player_process = None
            if parent_handle and parent_surface is not None:
                parent_surface.drop_exported_handle(parent_handle)
            print(f"Could not open clip player: {error}")
            self._show_toast(_("Could not play clip"))
            return

        if parent_handle and parent_surface is not None:
            process_id = self._player_process.pid
            self._exported_player_parents[process_id] = (parent_surface, parent_handle)
            GLib.child_watch_add(
                GLib.PRIORITY_DEFAULT,
                process_id,
                self._on_player_process_exited,
            )

    def _on_player_process_exited(self, process_id, _status):
        """Release the cross-process Wayland relationship with the player."""
        exported = self._exported_player_parents.pop(process_id, None)
        if exported is not None:
            surface, parent_handle = exported
            try:
                surface.drop_exported_handle(parent_handle)
            except Exception:
                pass
        process = getattr(self, "_player_process", None)
        if process is not None and getattr(process, "pid", None) == process_id:
            self._player_process = None
        GLib.spawn_close_pid(process_id)

    def on_reveal_clip(self, clip_data):
        """Reveal clip in file manager"""
        if self._open_containing_folder(clip_data["path"], "Could not reveal clip"):
            return

        try:
            # Try to use the file manager DBus interface to select the file
            subprocess.run(
                [
                    "dbus-send",
                    "--session",
                    "--dest=org.freedesktop.FileManager1",
                    "--type=method_call",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    f"array:string:file://{clip_data['path']}",
                    "string:",
                ],
                check=False,
                env=_clean_external_tool_env(),
            )
        except Exception:
            # Fallback: open the parent folder
            self._launch_path(clip_data["path"].parent, "Could not reveal clip")

    def _launch_path(self, path, error_message):
        """Open a file or folder through GTK, with a sanitized xdg-open fallback."""
        if hasattr(Gtk, "FileLauncher"):
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
            launcher.launch(
                self.get_root(),
                None,
                self._on_file_launch_finished,
                (Path(path), error_message),
            )
            return

        self._fallback_xdg_open(path, error_message)

    def _open_containing_folder(self, path, error_message):
        """Ask the file manager to reveal a file through GTK when available."""
        if not hasattr(Gtk, "FileLauncher"):
            return False

        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.open_containing_folder(
            self.get_root(),
            None,
            self._on_open_containing_folder_finished,
            (Path(path).parent, error_message),
        )
        return True

    def _on_file_launch_finished(self, launcher, result, user_data):
        path, error_message = user_data
        try:
            launcher.launch_finish(result)
        except Exception as e:
            print(f"{error_message}: {e}")
            self._fallback_xdg_open(path, error_message)

    def _on_open_containing_folder_finished(self, launcher, result, user_data):
        fallback_path, error_message = user_data
        try:
            launcher.open_containing_folder_finish(result)
        except Exception as e:
            print(f"{error_message}: {e}")
            self._fallback_xdg_open(fallback_path, error_message)

    def _fallback_xdg_open(self, path, error_message):
        """Use xdg-open without Flatpak Qt library overrides leaking into KDE tools."""
        try:
            result = subprocess.run(
                ["xdg-open", str(path)],
                check=False,
                env=_clean_external_tool_env(),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip().splitlines()
                if message:
                    print(f"{error_message}: {message[-1]}")
                self._show_toast(error_message)
        except Exception as e:
            print(f"{error_message}: {e}")
            self._show_toast(error_message)

    def on_delete_button_pressed(self, gesture, button, clip_data, row):
        """Arm shift-delete before the button's clicked handler runs."""
        state = gesture.get_current_event_state()
        if state & Gdk.ModifierType.SHIFT_MASK:
            button._shift_delete_armed = True
            self._delete_clip(clip_data, row, permanently=True)

    def on_delete_clip_clicked(self, button, clip_data, row):
        """Handle delete button activation, optionally bypassing confirmation."""
        if getattr(button, "_shift_delete_armed", False):
            button._shift_delete_armed = False
            return

        self.on_delete_clip(clip_data, row)

    def _delete_clip(self, clip_data, row, permanently=False):
        """Remove a clip from the UI and start background disposal."""
        clip_path = Path(clip_data["path"])
        self._locally_deleted_paths.add(clip_path)
        self._remove_clip_from_view(clip_path, row)

        thread = threading.Thread(
            target=self._delete_clip_file_worker,
            args=(clip_path, permanently),
            daemon=True,
        )
        thread.start()

    def on_delete_clip(self, clip_data, row):
        """Move the selected clip to trash after confirmation."""
        dialog = Adw.MessageDialog.new(self.get_root())
        dialog.set_heading(_("Move clip to trash?"))
        dialog.set_body(
            _("'%(name)s' will be moved to the trash.\nThis can be reversed.")
            % {"name": clip_data["name"]}
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to trash"))
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        dialog.connect("response", self.on_delete_confirmed, clip_data, row)
        dialog.present()

    def on_delete_confirmed(self, dialog, response, clip_data, row):
        """Handle trash confirmation."""
        if response == "trash":
            self._delete_clip(clip_data, row)

    def _remove_clip_from_view(self, clip_path, row):
        """Remove a clip row from the visible list without blocking on disk I/O."""
        try:
            self.clips_list.remove(row)
        except Exception:
            pass

        self.clips_data = [c for c in self.clips_data if c["path"] != clip_path]
        path = str(clip_path)
        self._clips_by_path.pop(path, None)
        self._clip_identities.pop(path, None)
        self._media_metadata.pop(path, None)
        self._render_clips()
        self._schedule_media_cache_save()

    def _delete_clip_file_worker(self, clip_path, permanently=False):
        """Trash or permanently delete a clip on a worker thread."""
        error = None
        try:
            if permanently:
                clip_path.unlink()
            else:
                trashed = Gio.File.new_for_path(str(clip_path)).trash(None)
                if not trashed:
                    raise OSError("The desktop trash service rejected the clip")
        except FileNotFoundError:
            pass
        except Exception as exc:
            error = exc

        GLib.idle_add(
            self._on_delete_clip_finished,
            clip_path,
            error,
            permanently,
        )

    def _on_delete_clip_finished(self, clip_path, error, permanently=False):
        if error is None:
            from editor_drafts import delete_editor_data

            delete_editor_data(clip_path)
            self._show_toast(_("Clip deleted") if permanently else _("Clip moved to trash"))
            self._schedule_forget_locally_deleted_path(clip_path)
        else:
            action = "deleting" if permanently else "moving clip to trash"
            print(f"Error {action}: {error}")
            self._locally_deleted_paths.discard(clip_path)
            self._show_toast(
                _("Could not delete clip")
                if permanently
                else _("Could not move clip to trash")
            )
            self.refresh(show_loading=True)
        return False

    def _schedule_forget_locally_deleted_path(self, clip_path):
        """Keep the path briefly so delayed monitor events do not reload the list."""
        if clip_path in self._local_delete_forget_ids:
            return

        add_timeout = getattr(GLib, "timeout_add_seconds", None)
        if add_timeout is not None:
            source_id = add_timeout(2, self._forget_locally_deleted_path, clip_path)
        else:
            source_id = GLib.timeout_add(2000, self._forget_locally_deleted_path, clip_path)
        self._local_delete_forget_ids[clip_path] = source_id

    def _forget_locally_deleted_path(self, clip_path):
        self._locally_deleted_paths.discard(clip_path)
        self._local_delete_forget_ids.pop(clip_path, None)
        return False

    def _show_toast(self, message):
        """Show a toast notification"""
        window = self.get_root()
        if window and hasattr(window, "show_toast"):
            window.show_toast(message)
