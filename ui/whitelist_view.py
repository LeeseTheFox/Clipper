"""
Whitelist view - manage games to capture
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

import game_capture
import steam
from capture_modes import (
    CAPTURE_MODE_GAME,
    CAPTURE_MODE_OPTIONS,
    DEFAULT_CAPTURE_MODE,
    capture_mode_for_entry,
    capture_mode_label,
    with_capture_mode,
)
from dialogs import (
    ProcessPickerDialog,
    SteamGamePickerDialog,
    show_steam_restart_dialog,
    show_warning_dialog,
)
from focus_helpers import new_id_dropdown
from game_icons import cached_icon_path_for_game, submit_icon_resolution
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from icon_names import GAME, PROCESS, SEARCH, STEAM, TRASH
from process_watcher import entries_share_executable_identity
from text_helpers import configure_single_line_ellipsis

_GAME_ICON_CSS_INSTALLED = False
_GAME_ARTWORK_WIDTH = 144
_GAME_ARTWORK_HEIGHT = 81
_GAME_ARTWORK_BORDER_WIDTH = 1
_GAME_PLACEHOLDER_ICON_SIZE = 48
_GAME_METADATA_ICON_SIZE = 18
_GAME_PLACEHOLDER_COLORS = (
    "green",
    "blue",
    "purple",
    "red",
    "yellow",
)
_STEAM_RESTART_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="clipper-steam-restart"
)


class _FixedGameArtwork(Gtk.Widget):
    """Constrain loaded artwork whose native dimensions are much larger."""

    def __init__(self, child: Gtk.Widget):
        super().__init__()
        self._child = child
        child.set_parent(self)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

    def do_measure(self, orientation, for_size):
        size = (
            _GAME_ARTWORK_WIDTH + (2 * _GAME_ARTWORK_BORDER_WIDTH)
            if orientation == Gtk.Orientation.HORIZONTAL
            else _GAME_ARTWORK_HEIGHT + (2 * _GAME_ARTWORK_BORDER_WIDTH)
        )
        return size, size, -1, -1

    def do_size_allocate(self, width, height, baseline):
        if self._child is not None:
            self._child.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot):
        if self._child is not None:
            self.snapshot_child(self._child, snapshot)

    def do_dispose(self):
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)


def _entry_display_name(game_data: dict, fallback: str | None = None) -> str:
    """Return a user-visible name for whitelist entries, including legacy manual ones."""
    for key in ("name", "executable_name", "executable_path", "path"):
        value = str(game_data.get(key) or "").strip()
        if not value:
            continue
        if key in {"executable_path", "path"} and ("/" in value or "\\" in value):
            value = Path(value.replace("\\", "/")).name
        if value:
            return value
    return fallback or _("This game")


def _entry_detail(game_data: dict) -> str:
    """Return the most useful path to show below a game's display name."""
    display_name = _entry_display_name(game_data, "")
    for key in ("install_path", "executable_path", "path"):
        value = str(game_data.get(key) or "").strip()
        if value and value != display_name:
            return value
    return ""


def _entry_matches_search(game_data: dict, query: str) -> bool:
    """Return whether a game matches a case-insensitive name/path search."""
    query = str(query or "").strip().casefold()
    if not query:
        return True

    searchable = (
        game_data.get("name"),
        game_data.get("executable_name"),
        game_data.get("executable_path"),
        game_data.get("install_path"),
        game_data.get("path"),
    )
    return any(query in str(value or "").casefold() for value in searchable)


def _placeholder_color_class(game_data: dict) -> str:
    """Return a stable, varied GNOME palette class for an entry."""
    identity = ""
    for key in (
        "appid",
        "executable_path",
        "path",
        "install_path",
        "name",
        "executable_name",
    ):
        value = str(game_data.get(key) or "").strip().casefold()
        if value:
            identity = f"{key}:{value}"
            break
    color_index = sha256(identity.encode("utf-8")).digest()[0] % len(_GAME_PLACEHOLDER_COLORS)
    return f"clipper-placeholder-{_GAME_PLACEHOLDER_COLORS[color_index]}"


def _install_game_icon_css() -> None:
    """Install local CSS for clipped game artwork icons."""
    global _GAME_ICON_CSS_INSTALLED
    if _GAME_ICON_CSS_INSTALLED:
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

        .clipper-game-artwork-icon {
            border-radius: 3px;
        }

        .clipper-game-placeholder {
            color: inherit;
        }

        .clipper-thumbnail.clipper-placeholder-blue {
            background-color: #dce8f8;
            color: #254e8d;
        }

        .clipper-thumbnail.clipper-placeholder-green {
            background-color: #dceedb;
            color: #3b7339;
        }

        .clipper-thumbnail.clipper-placeholder-yellow {
            background-color: #f8edc2;
            color: #7a5b00;
        }

        .clipper-thumbnail.clipper-placeholder-red {
            background-color: #f6dede;
            color: #942d2e;
        }

        .clipper-thumbnail.clipper-placeholder-purple {
            background-color: #e8def2;
            color: #523d72;
        }

        .clipper-dark .clipper-thumbnail.clipper-placeholder-green {
                background-color: #3b7339;
                color: #98d490;
        }

        .clipper-dark .clipper-thumbnail.clipper-placeholder-blue {
            background-color: #254e8d;
            color: #8bb2f9;
        }

        .clipper-dark .clipper-thumbnail.clipper-placeholder-purple {
            background-color: #523d72;
            color: #b999e1;
        }

        .clipper-dark .clipper-thumbnail.clipper-placeholder-red {
            background-color: #942d2e;
            color: #f98d8f;
        }

        .clipper-dark .clipper-thumbnail.clipper-placeholder-yellow {
            background-color: #b68e0c;
            color: #fbe56a;
        }

        @media (prefers-contrast: more) {
            .clipper-colored-placeholder {
                border-color: currentColor;
            }
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _GAME_ICON_CSS_INSTALLED = True


class WhitelistView(Gtk.Box):
    """View for managing whitelisted games"""

    def __init__(self, config=None, whitelist_changed_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(12)
        self.set_margin_bottom(12)

        self._config = config
        self._whitelist_changed_callback = whitelist_changed_callback
        self._style_manager = Adw.StyleManager.get_default()
        self._style_manager.connect("notify::dark", self._on_dark_style_changed)
        self._sync_dark_style_class()
        self.setup_ui()
        if self._config is not None and game_capture.BUNDLED_PAYLOAD.is_dir():
            GLib.idle_add(self._initialize_game_capture)

    def _on_dark_style_changed(self, _style_manager, _property) -> None:
        self._sync_dark_style_class()

    def _sync_dark_style_class(self) -> None:
        if self._style_manager.get_dark():
            self.add_css_class("clipper-dark")
        else:
            self.remove_css_class("clipper-dark")

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_whitelist(self):
        """Return whitelist entries from config (or default empty list if no config)."""
        if self._config is not None:
            return list(self._config.get("whitelist", []))
        # Empty list when running without a config
        return []

    def _save_whitelist(self):
        """Persist the current in-memory whitelist to config."""
        if self._config is not None:
            self._config.set("whitelist", self._whitelist)
        if self._whitelist_changed_callback is not None:
            self._whitelist_changed_callback()

    def reload_from_config(self, *, notify=True):
        """Rebuild the games list after the configuration is reset."""
        while row := self.games_list.get_first_child():
            self.games_list.remove(row)

        self._whitelist = []
        for entry in self._load_whitelist():
            self._add_entry(entry)

        self._update_content_state()

        if notify and self._whitelist_changed_callback is not None:
            self._whitelist_changed_callback()

    def _new_capture_mode_dropdown(self) -> Gtk.DropDown:
        return new_id_dropdown(CAPTURE_MODE_OPTIONS, DEFAULT_CAPTURE_MODE)

    def _new_game_artwork(self, game_data: dict):
        artwork_frame = Gtk.Box()
        artwork_frame.set_overflow(Gtk.Overflow.HIDDEN)
        artwork_frame.add_css_class("clipper-thumbnail")

        artwork_overlay = Gtk.Overlay()
        artwork_overlay.set_hexpand(True)
        artwork_overlay.set_vexpand(True)
        artwork_frame.append(artwork_overlay)
        artwork = _FixedGameArtwork(artwork_frame)

        picture = Gtk.Picture()
        picture.set_size_request(_GAME_ARTWORK_WIDTH, _GAME_ARTWORK_HEIGHT)
        picture.set_can_shrink(True)
        picture.set_content_fit(
            Gtk.ContentFit.COVER if "appid" in game_data else Gtk.ContentFit.CONTAIN
        )
        picture.add_css_class("clipper-thumbnail-image")
        artwork_overlay.set_child(picture)

        placeholder_icon_name = STEAM if "appid" in game_data else GAME
        placeholder = Gtk.Image.new_from_icon_name(placeholder_icon_name)
        placeholder.set_pixel_size(_GAME_PLACEHOLDER_ICON_SIZE)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.add_css_class("clipper-game-placeholder")
        artwork_overlay.add_overlay(placeholder)

        artwork_path = self._game_artwork_path(game_data)
        if artwork_path:
            picture.set_filename(artwork_path)
            placeholder.set_visible(False)
        else:
            placeholder_color_class = _placeholder_color_class(game_data)
            artwork_frame.add_css_class("clipper-colored-placeholder")
            artwork_frame.add_css_class(placeholder_color_class)
            placeholder._clipper_color_class = placeholder_color_class
            picture.set_visible(False)
        return artwork, artwork_frame, picture, placeholder

    @staticmethod
    def _game_artwork_path(game_data: dict) -> str:
        if "appid" in game_data:
            return steam.get_game_header_path(str(game_data.get("appid") or ""))
        return cached_icon_path_for_game(game_data)

    def _new_action_icon(self, icon_name: str, size: int = _GAME_METADATA_ICON_SIZE) -> Gtk.Image:
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(size)
        icon.add_css_class("dim-label")
        return icon

    def _load_game_icon_async(
        self,
        game_data: dict,
        artwork_frame: Gtk.Box,
        picture: Gtk.Picture,
        placeholder: Gtk.Image,
    ) -> None:
        if "appid" in game_data or cached_icon_path_for_game(game_data):
            return

        future = submit_icon_resolution(game_data)
        future.add_done_callback(
            lambda completed: GLib.idle_add(
                self._apply_resolved_game_icon,
                game_data,
                artwork_frame,
                picture,
                placeholder,
                completed,
            )
        )

    def _apply_resolved_game_icon(
        self,
        game_data: dict,
        artwork_frame: Gtk.Box,
        picture: Gtk.Picture,
        placeholder: Gtk.Image,
        future,
    ):
        try:
            executable_path, icon_path = future.result()
        except Exception:  # noqa: BLE001
            return False

        if executable_path:
            game_data["executable_path"] = executable_path
        if not icon_path:
            return False

        game_data["icon_path"] = icon_path
        picture.set_filename(icon_path)
        picture.set_visible(True)
        placeholder.set_visible(False)
        placeholder_color_class = getattr(placeholder, "_clipper_color_class", "")
        if placeholder_color_class:
            artwork_frame.remove_css_class("clipper-colored-placeholder")
            artwork_frame.remove_css_class(placeholder_color_class)
        return False

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def setup_ui(self):
        """Build the whitelist view UI"""
        _install_game_icon_css()

        # Keep an in-memory mirror of whitelist rows so we can serialise them.
        self._whitelist: list[dict] = []

        # Match the Clips view's compact control strip and keep the primary
        # content below it, rather than anchoring large actions to the bottom.
        controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls_box.set_margin_start(6)
        controls_box.set_margin_end(6)
        controls_box.set_margin_bottom(12)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_width_chars(12)
        self.search_entry.set_placeholder_text(_("Search games by name or path"))
        self.search_entry.connect("search-changed", self.on_search_changed)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_search_key_pressed)
        self.search_entry.add_controller(key_controller)
        controls_box.append(self.search_entry)

        self._add_action_group = Gio.SimpleActionGroup()
        steam_action = Gio.SimpleAction.new("add-steam", None)
        steam_action.connect("activate", lambda *_args: self.on_add_steam_game(None))
        self._add_action_group.add_action(steam_action)
        manual_action = Gio.SimpleAction.new("add-manually", None)
        manual_action.connect("activate", lambda *_args: self.on_process_picker(None))
        self._add_action_group.add_action(manual_action)
        self.insert_action_group("games", self._add_action_group)

        add_menu = Gio.Menu()
        steam_item = Gio.MenuItem.new(_("Add a Steam game"), "games.add-steam")
        steam_item.set_icon(Gio.ThemedIcon.new(STEAM))
        add_menu.append_item(steam_item)
        manual_item = Gio.MenuItem.new(_("Add a non-Steam game"), "games.add-manually")
        manual_item.set_icon(Gio.ThemedIcon.new(PROCESS))
        add_menu.append_item(manual_item)

        add_button = Gtk.MenuButton(label=_("Add new"))
        add_button.set_always_show_arrow(True)
        add_button.set_menu_model(add_menu)
        add_button.set_halign(Gtk.Align.END)
        add_button.add_css_class("suggested-action")

        self.trailing_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.trailing_controls.set_valign(Gtk.Align.CENTER)
        self.trailing_controls.append(add_button)
        controls_box.append(self.trailing_controls)
        self.append(controls_box)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_vexpand(True)
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.games_list = Gtk.ListBox()
        self.games_list.add_css_class("boxed-list")
        self.games_list.set_margin_start(6)
        self.games_list.set_margin_end(6)
        self.games_list.set_margin_top(6)
        self.games_list.set_margin_bottom(6)
        self.games_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.games_list.set_can_focus(False)
        self.games_list.set_filter_func(self._filter_game_row)
        self.games_list.connect("row-activated", self._on_game_activated)
        self.scrolled.set_child(self.games_list)
        self.content_stack.add_named(self.scrolled, "games")

        self.empty_state = self._create_status_page(
            GAME,
            "No games added",
            "Add a Steam game or choose a running game to start capturing it.",
        )
        self.content_stack.add_named(self.empty_state, "empty")

        self.no_results_state = self._create_status_page(
            SEARCH,
            "No matching games",
            "Try a different name or executable path.",
        )
        self.content_stack.add_named(self.no_results_state, "no-results")

        self.append(self.content_stack)

        # Populate from config (or stubs)
        for entry in self._load_whitelist():
            self._add_entry(entry)
        self._update_content_state()

    @staticmethod
    def _create_status_page(icon_name: str, title: str, description: str):
        status_page = Adw.StatusPage()
        status_page.set_icon_name(icon_name)
        status_page.set_title(title)
        status_page.set_description(description)
        return status_page

    def _on_search_key_pressed(self, controller, keyval, keycode, state):
        """Clear keyboard focus from the search entry when Escape is pressed."""
        if keyval != Gdk.KEY_Escape:
            return False
        root = self.search_entry.get_root()
        if root is not None and hasattr(root, "set_focus"):
            root.set_focus(None)
        return True

    def on_search_changed(self, search_entry):
        """Filter configured games as the search query changes."""
        self.games_list.invalidate_filter()
        self._update_content_state()

    def _filter_game_row(self, row) -> bool:
        game_data = getattr(row, "_clipper_game_data", {})
        return _entry_matches_search(game_data, self.search_entry.get_text())

    def _update_content_state(self) -> None:
        query = self.search_entry.get_text()
        if not self._whitelist:
            child_name = "empty"
        elif query.strip() and not any(
            _entry_matches_search(game_data, query) for game_data in self._whitelist
        ):
            child_name = "no-results"
        else:
            child_name = "games"
        self.content_stack.set_visible_child_name(child_name)

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _add_entry(self, game_data: dict):
        """Add an entry to the in-memory list and the visible list."""
        game_data = with_capture_mode(game_data, capture_mode_for_entry(game_data))
        self._whitelist.append(game_data)
        row = self._create_game_row(game_data)
        self.games_list.append(row)
        self._update_content_state()

    def _duplicate_entry_for(self, game_data: dict) -> dict | None:
        """Return an existing entry that describes the same executable/game."""
        for entry in self._whitelist:
            if entries_share_executable_identity(game_data, entry):
                return entry
        return None

    def _show_duplicate_entry_warning(self, game_data: dict, duplicate: dict) -> None:
        name = _entry_display_name(game_data, _entry_display_name(duplicate))
        show_warning_dialog(
            self.get_root(),
            _("%(name)s is already in the whitelist.") % {"name": name},
        )

    def _create_game_row(self, game_data):
        """Create a row for a whitelisted game"""
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row.set_selectable(False)
        row._clipper_game_data = game_data

        # Match the painted Clips-row anchors. The fixed artwork wrapper needs
        # one extra leading pixel, and its following text needs one extra pixel
        # of separation, to align with the direct Gtk.Box clip thumbnail.
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=13)
        row_box.set_margin_start(13)
        row_box.set_margin_end(12)
        row_box.set_margin_top(12)
        row_box.set_margin_bottom(12)

        artwork, artwork_frame, picture, placeholder = self._new_game_artwork(game_data)
        row_box.append(artwork)
        self._load_game_icon_async(game_data, artwork_frame, picture, placeholder)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_valign(Gtk.Align.CENTER)
        info_box.set_hexpand(True)

        display_name = _entry_display_name(game_data)
        name_label = Gtk.Label(label=display_name)
        name_label.set_halign(Gtk.Align.START)
        name_label.set_hexpand(True)
        name_label.set_xalign(0)
        name_label.add_css_class("heading")
        configure_single_line_ellipsis(name_label, max_width_chars=40)
        info_box.append(name_label)

        capture_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        capture_box.set_halign(Gtk.Align.START)
        capture_box.set_valign(Gtk.Align.CENTER)

        source_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        source_box.set_valign(Gtk.Align.CENTER)
        source_box.add_css_class("clipper-metadata-chip")

        source_icon_name = STEAM if "appid" in game_data else PROCESS
        source_icon = self._new_action_icon(source_icon_name)
        source_box.append(source_icon)

        source = _("Steam") if "appid" in game_data else _("Manual")
        source_label = Gtk.Label(label=source)
        source_label.add_css_class("caption")
        source_box.append(source_label)
        capture_box.append(source_box)

        method = capture_mode_label(capture_mode_for_entry(game_data))
        capture_label = Gtk.Label(label=method)
        capture_label.set_halign(Gtk.Align.START)
        capture_label.set_xalign(0)
        capture_label.add_css_class("caption")
        capture_label.add_css_class("clipper-accent-chip")
        capture_box.append(capture_label)
        info_box.append(capture_box)

        detail = _entry_detail(game_data)
        if detail:
            detail_label = Gtk.Label(label=detail)
            detail_label.set_halign(Gtk.Align.START)
            detail_label.set_hexpand(True)
            detail_label.set_xalign(0)
            detail_label.add_css_class("caption")
            detail_label.add_css_class("dim-label")
            configure_single_line_ellipsis(detail_label)
            info_box.append(detail_label)

        row_box.append(info_box)

        remove_button = Gtk.Button.new_from_icon_name(TRASH)
        remove_button.set_valign(Gtk.Align.CENTER)
        remove_button.set_tooltip_text(_("Remove %(name)s") % {"name": display_name})
        remove_button.add_css_class("destructive-action")
        remove_button.connect("clicked", lambda button: self.on_remove_game(button, row))
        row_box.append(remove_button)

        row.set_child(row_box)
        return row

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_game_activated(self, _list, row):
        from game_details_dialog import GameDetailsDialog

        entry = row._clipper_game_data
        artwork, frame, picture, placeholder = self._new_game_artwork(entry)
        self._load_game_icon_async(dict(entry), frame, picture, placeholder)
        dialog = GameDetailsDialog(
            entry, _entry_display_name(entry), artwork, self._save_game_executable
        )
        dialog.present(self.get_root())

    def _save_game_executable(self, original, path):
        from game_details import replace_executable

        entries = self._load_whitelist() if self._config is not None else self._whitelist
        updated = replace_executable(entries, original, path)
        if self._config is not None:
            self._config.set("whitelist", updated)
            self.reload_from_config()
        else:
            while row := self.games_list.get_first_child():
                self.games_list.remove(row)
            self._whitelist = []
            for entry in updated:
                self._add_entry(entry)
            self._update_content_state()
        self._show_toast(_("Executable updated"))

    def on_add_steam_game(self, button):
        """Show Steam game picker dialog"""
        window = self.get_root()
        dialog = SteamGamePickerDialog(window)
        dialog.connect("game-selected", self.on_steam_game_selected)
        dialog.present()

    def on_process_picker(self, button):
        """Show process picker dialog"""
        window = self.get_root()
        dialog = ProcessPickerDialog(window)
        dialog.connect("process-selected", self.on_process_selected)
        dialog.present()

    def on_process_selected(self, dialog, process_name, process_path, capture_mode):
        """Handle process selection from picker"""
        process_path = str(process_path or process_name or "").strip()
        process_name = str(process_name or "").strip()
        if not process_name:
            process_name = _entry_display_name({"path": process_path})

        game_data = {"name": process_name, "path": process_path}
        game_data.update(getattr(dialog, "selection_identity", {}))
        if "/" in process_path:
            executable_name = Path(process_path).name
            if executable_name:
                game_data["executable_name"] = executable_name
                game_data["executable_path"] = process_path
        game_data = with_capture_mode(game_data, capture_mode)
        duplicate = self._duplicate_entry_for(game_data)
        if duplicate is not None:
            self._show_duplicate_entry_warning(game_data, duplicate)
            return

        if capture_mode == CAPTURE_MODE_GAME:
            future = _STEAM_RESTART_EXECUTOR.submit(game_capture.prepare)
            future.add_done_callback(
                lambda result: GLib.idle_add(self._finish_manual_capture, result, game_data)
            )
        else:
            self._finish_add_game(game_data)

    def _finish_manual_capture(self, future: Future, game_data: dict) -> bool:
        try:
            future.result()
        except Exception as exc:
            self._change_failed(exc)
            return False
        self._finish_add_game(game_data)
        return False

    def on_steam_game_selected(self, dialog, game_data):
        """Add the selected installation/account, preparing hooks in the background."""
        game_data = with_capture_mode(game_data, game_data.get("capture_mode"))
        duplicate = self._duplicate_entry_for(game_data)
        if duplicate is not None:
            self._show_duplicate_entry_warning(game_data, duplicate)
            return
        if capture_mode_for_entry(game_data) != CAPTURE_MODE_GAME:
            self._finish_add_game(game_data)
            return
        self._change_steam_game(game_data, remove=False)

    def _finish_add_game(self, game_data: dict) -> None:
        self._add_entry(game_data)
        self._save_whitelist()
        self._show_toast(_("Added %(name)s") % {"name": _entry_display_name(game_data)})

    def on_remove_game(self, button, row):
        if row is None or getattr(self, "_steam_change_pending", False):
            return
        game_data = row._clipper_game_data
        if "appid" in game_data and capture_mode_for_entry(game_data) == CAPTURE_MODE_GAME:
            self._change_steam_game(game_data, remove=True)
        else:
            self._finish_remove_game(game_data, row)

    def _finish_remove_game(self, game_data: dict, row) -> None:
        self.games_list.remove(row)
        self._whitelist.remove(game_data)
        self._save_whitelist()
        self._update_content_state()
        self._show_toast(_("Removed %(name)s") % {"name": _entry_display_name(game_data)})

    def _change_steam_game(self, game_data: dict, *, remove: bool) -> None:
        if getattr(self, "_steam_change_pending", False):
            return
        self._steam_change_pending = True
        self.set_sensitive(False)

        def prepare():
            installation = steam.installation_for_entry(game_data)
            entry = dict(game_data)
            entry["steam_installation"] = installation.stable_id
            entry["steam_environment"] = installation.environment.value
            entry["steam_account_id"] = steam.select_steam_account(
                installation.data_root, entry.get("steam_account_id")
            ).account_id
            if not remove:
                game_capture.prepare(installation.environment)
            running = steam._is_steam_running(environment=installation.environment)
            return installation, entry, running

        def prepared(future):
            try:
                installation, entry, running = future.result()
            except Exception as exc:
                self._change_failed(exc)
                return False

            def apply():
                self.set_sensitive(False)

                def commit():
                    steam.recover_capture_change(self._config)
                    if remove:
                        steam.remove_game_capture_entry(
                            self._config, game_data, entry, installation
                        )
                    else:
                        steam.apply_game_capture_add_transaction(
                            self._config,
                            entry,
                            str(game_capture.wrapper_path()),
                            installation.data_root,
                        )

                def work():
                    if running:
                        steam.restart_steam_around(commit, installation.environment)
                    else:
                        commit()

                completed = _STEAM_RESTART_EXECUTOR.submit(work)
                completed.add_done_callback(
                    lambda result: GLib.idle_add(self._finish_steam_change, result, entry, remove)
                )

            if running:
                self.set_sensitive(True)
                show_steam_restart_dialog(
                    self.get_root(), apply, on_cancel=self._cancel_steam_change
                )
            else:
                apply()
            return False

        future = _STEAM_RESTART_EXECUTOR.submit(prepare)
        future.add_done_callback(lambda result: GLib.idle_add(prepared, result))

    def _cancel_steam_change(self):
        self._steam_change_pending = False
        self.set_sensitive(True)

    def _initialize_game_capture(self) -> bool:
        """Refresh exported hooks and upgrade existing games after an app update."""
        if self._config is None:
            return False
        if not self._config.get("pending_game_capture_change") and not any(
            capture_mode_for_entry(entry) == CAPTURE_MODE_GAME for entry in self._whitelist
        ):
            return False
        self._steam_change_pending = True
        self.set_sensitive(False)
        future = _STEAM_RESTART_EXECUTOR.submit(steam.capture_updates, self._config)
        future.add_done_callback(lambda result: GLib.idle_add(self._apply_capture_updates, result))
        return False

    def _apply_capture_updates(self, future: Future) -> bool:
        try:
            updates = future.result()
        except Exception as exc:
            self._change_failed(exc)
            return False
        if not updates:
            self._cancel_steam_change()
            return False

        def apply():
            self.set_sensitive(False)
            completed = _STEAM_RESTART_EXECUTOR.submit(
                steam.apply_capture_updates, self._config, updates
            )
            completed.add_done_callback(lambda result: GLib.idle_add(finished, result))

        def finished(result):
            self._cancel_steam_change()
            self.reload_from_config()
            try:
                result.result()
            except Exception as exc:
                self._change_failed(exc)
            return False

        if any(running for _entry, _installation, running in updates):
            self.set_sensitive(True)
            show_steam_restart_dialog(self.get_root(), apply, on_cancel=self._cancel_steam_change)
        else:
            apply()
        return False

    def _change_failed(self, error):
        self._cancel_steam_change()
        show_warning_dialog(
            self.get_root(),
            _("Could not finish the Game capture change.\n\n%(error)s") % {"error": error},
        )

    def _finish_steam_change(self, future: Future, entry: dict, remove: bool) -> bool:
        self._cancel_steam_change()
        try:
            future.result()
        except Exception as exc:
            self.reload_from_config()
            self._change_failed(exc)
            return False
        self.reload_from_config()
        message = _("Removed %(name)s") if remove else _("Added %(name)s")
        self._show_toast(message % {"name": _entry_display_name(entry)})
        return False

    def _show_toast(self, message):
        """Show a toast notification"""
        window = self.get_root()
        if window and hasattr(window, "show_toast"):
            window.show_toast(message)
