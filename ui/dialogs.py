"""
Dialog windows for Clipper
"""

from pathlib import Path

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import steam
from capture_modes import CAPTURE_MODE_GAME, CAPTURE_MODE_OPTIONS, DEFAULT_CAPTURE_MODE
from focus_helpers import dropdown_active_id, new_id_dropdown
from game_icons import cached_icon_path_for_game, submit_icon_resolution
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango
from icon_names import (
    CHECKMARK,
    COPY,
    FOLDER_OPEN,
    GAME,
    INFO_CIRCLE_BOLD,
    STEAM,
    WARNING,
)
from monitor_manager import HostMonitorManager
from process_watcher import process_choices, running_process_choices
from text_helpers import configure_single_line_ellipsis

CAPTURE_METHOD_HELP_TEXT = _(
    "Screen capture starts capturing your whole selected screen when an added "
    "game is running.\n\n"
    "Game capture adds a special flag to the game's launch options so Clipper "
    "can hook into the game's direct Vulkan output and capture only the game "
    "window."
)
NON_STEAM_CAPTURE_METHOD_HELP_TEXT = _(
    "Display capture records your selected screen while this game is running.\n\n"
    "For non-Steam games, Game capture requires adding a launch option to "
    "your game's launcher. Select it to see and copy the required launch "
    "option. This lets Clipper hook into the game's direct Vulkan output "
    "and capture only the game window."
)
_GAME_ICON_CSS_INSTALLED = False
_PROCESS_PICKER_WIDTH = 400
_PROCESS_PICKER_DEFAULT_HEIGHT = 500


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
        .clipper-game-artwork-icon {
            border-radius: 3px;
        }
        """
    )
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _GAME_ICON_CSS_INSTALLED = True


def _style_game_artwork_icon(icon: Gtk.Image) -> None:
    icon.add_css_class("clipper-game-artwork-icon")
    icon.set_overflow(Gtk.Overflow.HIDDEN)


def _new_capture_mode_dropdown() -> Gtk.DropDown:
    return new_id_dropdown(
        CAPTURE_MODE_OPTIONS,
        DEFAULT_CAPTURE_MODE,
        max_width_chars=18,
        width_request=180,
    )


def _new_capture_method_row(
    help_text: str = CAPTURE_METHOD_HELP_TEXT,
) -> tuple[Gtk.Box, Gtk.DropDown]:
    capture_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    capture_box.set_margin_start(12)
    capture_box.set_margin_end(12)
    capture_box.set_margin_top(12)

    capture_label = Gtk.Label(label=_("Capture method"))
    capture_label.set_halign(Gtk.Align.START)
    capture_label.set_hexpand(True)
    capture_box.append(capture_label)

    info_icon = Gtk.Image.new_from_icon_name(INFO_CIRCLE_BOLD)
    info_icon.set_tooltip_text(help_text)
    info_icon.set_pixel_size(16)
    info_icon.set_halign(Gtk.Align.CENTER)
    info_icon.set_valign(Gtk.Align.CENTER)
    info_icon.add_css_class("dim-label")
    capture_box.append(info_icon)

    capture_mode_dropdown = _new_capture_mode_dropdown()
    capture_box.append(capture_mode_dropdown)
    return capture_box, capture_mode_dropdown


def _new_game_capture_launch_option() -> tuple[Gtk.Box, Gtk.Entry, Gtk.Button]:
    launch_option_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    launch_option_box.set_margin_start(12)
    launch_option_box.set_margin_end(12)
    launch_option_box.set_margin_top(12)

    launch_option_label = Gtk.Label(label=_("Required launch option"))
    launch_option_label.set_halign(Gtk.Align.START)
    launch_option_label.set_xalign(0)
    launch_option_label.add_css_class("heading")
    launch_option_box.append(launch_option_label)

    launch_option_description = Gtk.Label(
        label=_("Copy this into the game's launch options before starting it.")
    )
    launch_option_description.set_halign(Gtk.Align.START)
    launch_option_description.set_xalign(0)
    launch_option_description.set_wrap(True)
    launch_option_description.add_css_class("dim-label")
    launch_option_description.add_css_class("caption")
    launch_option_box.append(launch_option_description)

    launch_option_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

    launch_option_entry = Gtk.Entry()
    launch_option_entry.set_hexpand(True)
    launch_option_entry.set_editable(False)
    launch_option_entry.set_text(steam.CLIPPER_WRAPPER)
    launch_option_entry.set_tooltip_text(_("Copy the required launch option"))
    launch_option_row.append(launch_option_entry)

    copy_button = Gtk.Button.new_from_icon_name(COPY)
    copy_button.set_tooltip_text(_("Copy launch option"))
    launch_option_row.append(copy_button)

    launch_option_box.append(launch_option_row)
    return launch_option_box, launch_option_entry, copy_button


def _new_game_icon(game_data: dict) -> Gtk.Image:
    icon_path = cached_icon_path_for_game(game_data)
    if icon_path:
        icon = Gtk.Image.new_from_file(icon_path)
        _style_game_artwork_icon(icon)
    else:
        icon_name = STEAM if "appid" in game_data else GAME
        icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(32)
    icon.set_valign(Gtk.Align.CENTER)
    return icon


def _apply_resolved_game_icon(game_data: dict, icon: Gtk.Image, future):
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
    _style_game_artwork_icon(icon)
    icon.set_pixel_size(32)
    return False


def _load_game_icon_async(game_data: dict, icon: Gtk.Image) -> None:
    if cached_icon_path_for_game(game_data):
        return

    future = submit_icon_resolution(game_data)
    future.add_done_callback(
        lambda completed: GLib.idle_add(_apply_resolved_game_icon, game_data, icon, completed)
    )


class ProcessPickerDialog(Gtk.Window):
    """Dialog for picking a running process or executable file"""

    __gsignals__ = {"process-selected": (GObject.SIGNAL_RUN_FIRST, None, (str, str, str))}

    def __init__(self, parent):
        super().__init__()
        self.set_title(_("Add a non-Steam game"))
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(_PROCESS_PICKER_WIDTH, _PROCESS_PICKER_DEFAULT_HEIGHT)
        self.set_resizable(False)

        # Add ESC key handler
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        self.setup_ui()

    def setup_ui(self):
        """Build the process picker UI"""
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header bar
        header = Adw.HeaderBar()
        self.set_titlebar(header)

        process_label = Gtk.Label(label=_("Pick a process"))
        process_label.set_halign(Gtk.Align.START)
        process_label.set_margin_start(12)
        process_label.set_margin_end(12)
        process_label.set_margin_top(12)
        process_label.add_css_class("heading")
        main_box.append(process_label)

        # Search bar
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search processes…"))
        self.search_entry.set_margin_start(12)
        self.search_entry.set_margin_end(12)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(12)
        self.search_entry.connect("search-changed", self.on_search_changed)
        main_box.append(self.search_entry)

        # Process list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(6)
        scrolled.set_margin_end(6)

        self.process_list = Gtk.ListBox()
        self.process_list.add_css_class("boxed-list")
        self.process_list.set_margin_start(6)
        self.process_list.set_margin_end(6)
        self.process_list.set_margin_top(6)
        self.process_list.set_margin_bottom(6)
        self.process_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.process_list.connect("row-selected", self.on_process_row_selected)
        scrolled.set_child(self.process_list)

        self.process_data_list = []
        self.populate_processes()

        main_box.append(scrolled)

        executable_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        executable_box.set_margin_start(12)
        executable_box.set_margin_end(12)
        executable_box.set_margin_top(12)

        executable_label = Gtk.Label(label=_("Or pick an executable path"))
        executable_label.set_halign(Gtk.Align.START)
        executable_label.add_css_class("heading")
        executable_box.append(executable_label)

        executable_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self.executable_entry = Gtk.Entry()
        self.executable_entry.set_hexpand(True)
        self.executable_entry.set_placeholder_text(_("/path/to/game.exe"))
        self.executable_entry.connect("activate", self.on_select_clicked)
        self.executable_entry.connect("changed", self.on_executable_entry_changed)
        executable_row.append(self.executable_entry)

        executable_button = Gtk.Button.new_from_icon_name(FOLDER_OPEN)
        executable_button.set_tooltip_text(_("Choose executable"))
        executable_button.connect("clicked", self.on_choose_executable_clicked)
        executable_row.append(executable_button)

        executable_box.append(executable_row)
        main_box.append(executable_box)

        capture_box, self.capture_mode_dropdown = _new_capture_method_row(
            NON_STEAM_CAPTURE_METHOD_HELP_TEXT
        )
        main_box.append(capture_box)

        (
            self.game_capture_launch_option_box,
            self.game_capture_launch_option_entry,
            self.game_capture_launch_option_copy_button,
        ) = _new_game_capture_launch_option()
        self.game_capture_launch_option_copy_button.connect(
            "clicked", self._on_copy_game_capture_launch_option
        )
        self._copy_feedback_timeout_id = None
        self.capture_mode_dropdown.connect(
            "notify::selected", self._on_capture_mode_changed
        )
        main_box.append(self.game_capture_launch_option_box)
        self._update_game_capture_launch_option_visibility()

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_margin_start(12)
        button_box.set_margin_end(12)
        button_box.set_margin_top(12)
        button_box.set_margin_bottom(12)
        button_box.set_halign(Gtk.Align.CENTER)

        self.select_button = Gtk.Button(label=_("Add selected"))
        self.select_button.add_css_class("suggested-action")
        self.select_button.connect("clicked", self.on_select_clicked)
        button_box.append(self.select_button)

        main_box.append(button_box)

        self.set_child(main_box)
        self.update_process_select_button_state()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events"""
        if keyval == Gdk.KEY_Escape and not state:
            self.close()
            return True
        return False

    def populate_processes(self):
        """Add host processes in Flatpak, or local processes in native builds."""
        monitor_manager = HostMonitorManager()
        load_failed = False
        if monitor_manager.available():
            host_processes = monitor_manager.list_processes()
            load_failed = host_processes is None
            self.process_data_list = (
                process_choices(host_processes) if host_processes is not None else []
            )
        else:
            self.process_data_list = running_process_choices()

        if not self.process_data_list:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            label = Gtk.Label(
                label=(
                    _("Could not load host processes")
                    if load_failed
                    else _("No running processes found")
                )
            )
            label.set_margin_start(12)
            label.set_margin_end(12)
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            label.add_css_class("dim-label")
            row.set_child(label)
            self.process_list.append(row)
            return

        for process in self.process_data_list:
            self.process_list.append(self.create_process_row(process))

    def create_process_row(self, process):
        """Create a row for one running process."""
        row = Gtk.ListBoxRow()

        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)

        name_label = Gtk.Label(label=process["name"])
        name_label.set_halign(Gtk.Align.START)
        name_label.set_xalign(0)
        configure_single_line_ellipsis(name_label, mode=Pango.EllipsizeMode.END)
        name_label.add_css_class("heading")
        row_box.append(name_label)

        detail_parts = []
        if process.get("pid"):
            detail_parts.append(f"PID {process['pid']}")
        path = process.get("path", "")
        if path and path != process["name"]:
            detail_parts.append(path)
        if detail_parts:
            detail_label = Gtk.Label(label=" - ".join(detail_parts))
            detail_label.set_halign(Gtk.Align.START)
            detail_label.set_xalign(0)
            configure_single_line_ellipsis(
                detail_label, mode=Pango.EllipsizeMode.MIDDLE
            )
            detail_label.add_css_class("dim-label")
            detail_label.add_css_class("caption")
            row_box.append(detail_label)

        row.set_child(row_box)
        return row

    def on_search_changed(self, entry):
        """Filter process list based on search"""
        search_text = entry.get_text().lower()

        def filter_func(row):
            index = row.get_index()
            if index < 0 or index >= len(self.process_data_list):
                return False
            process = self.process_data_list[index]
            searchable = " ".join(
                (
                    process.get("name", ""),
                    process.get("path", ""),
                    process.get("cmdline", ""),
                )
            ).lower()
            return search_text in searchable

        self.process_list.set_filter_func(filter_func)
        selected_row = self.process_list.get_selected_row()
        if selected_row is not None and not filter_func(selected_row):
            self.process_list.unselect_row(selected_row)
        self.update_process_select_button_state()

    def on_process_row_selected(self, listbox, row):
        """Refresh the select button when a running process row is selected."""
        self.update_process_select_button_state()

    def on_executable_entry_changed(self, entry):
        """Refresh the select button when the executable path changes."""
        self.update_process_select_button_state()

    def _selected_capture_mode(self) -> str:
        capture_mode = dropdown_active_id(
            self.capture_mode_dropdown, DEFAULT_CAPTURE_MODE
        )
        return capture_mode if isinstance(capture_mode, str) else DEFAULT_CAPTURE_MODE

    def _on_capture_mode_changed(self, dropdown, _property) -> None:
        self._update_game_capture_launch_option_visibility()

    def _on_copy_game_capture_launch_option(self, _button) -> None:
        self.get_display().get_clipboard().set(
            self.game_capture_launch_option_entry.get_text()
        )
        self.game_capture_launch_option_copy_button.set_icon_name(CHECKMARK)
        self.game_capture_launch_option_copy_button.set_tooltip_text(_("Copied"))
        self.game_capture_launch_option_copy_button.add_css_class("suggested-action")

        if self._copy_feedback_timeout_id is not None:
            GLib.source_remove(self._copy_feedback_timeout_id)
        self._copy_feedback_timeout_id = GLib.timeout_add(
            1500, self._reset_copy_game_capture_feedback
        )

    def _reset_copy_game_capture_feedback(self) -> bool:
        self._copy_feedback_timeout_id = None
        self.game_capture_launch_option_copy_button.set_icon_name(COPY)
        self.game_capture_launch_option_copy_button.set_tooltip_text(
            _("Copy launch option")
        )
        self.game_capture_launch_option_copy_button.remove_css_class(
            "suggested-action"
        )
        return GLib.SOURCE_REMOVE

    def _update_game_capture_launch_option_visibility(self) -> None:
        game_capture_selected = self._selected_capture_mode() == CAPTURE_MODE_GAME
        self.game_capture_launch_option_box.set_visible(game_capture_selected)

        launch_option_height = 0
        if game_capture_selected:
            _, launch_option_height, _, _ = (
                self.game_capture_launch_option_box.measure(
                    Gtk.Orientation.VERTICAL, _PROCESS_PICKER_WIDTH
                )
            )
        self.set_default_size(
            _PROCESS_PICKER_WIDTH,
            _PROCESS_PICKER_DEFAULT_HEIGHT + launch_option_height,
        )

    def _selected_process(self) -> dict | None:
        selected_row = self.process_list.get_selected_row()
        if selected_row is None:
            return None

        index = selected_row.get_index()
        if 0 <= index < len(self.process_data_list):
            return self.process_data_list[index]
        return None

    def update_process_select_button_state(self):
        """Enable selection only when a process or executable path is available."""
        if not hasattr(self, "select_button"):
            return

        has_executable_path = bool(self.executable_entry.get_text().strip())
        self.select_button.set_sensitive(
            has_executable_path or self._selected_process() is not None
        )

    def on_choose_executable_clicked(self, button):
        """Open an executable file chooser."""
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose game executable"))
        dialog.set_accept_label(_("Choose"))
        dialog.open(self, None, self._on_executable_chosen)

    def _on_executable_chosen(self, dialog, result):
        """Fill the executable field with the chosen file."""
        try:
            executable = dialog.open_finish(result)
        except GLib.Error:
            return

        if not executable:
            return

        executable_path = executable.get_path()
        if not executable_path:
            show_warning_dialog(self, _("Choose a local executable file."))
            return

        self.executable_entry.set_text(executable_path)

    def _select_executable_path(self, executable_path: str) -> bool:
        executable_path = executable_path.strip()
        if not executable_path:
            return False

        process_name = Path(executable_path).name
        if not process_name:
            show_warning_dialog(self, _("Choose a valid executable file."))
            return False

        self.emit(
            "process-selected",
            process_name,
            executable_path,
            self._selected_capture_mode(),
        )
        self.close()
        return True

    def on_select_clicked(self, button):
        """Emit signal with selected process"""
        executable_path = self.executable_entry.get_text().strip()
        if executable_path:
            self._select_executable_path(executable_path)
            return

        process = self._selected_process()
        if process is not None:
            self.emit(
                "process-selected",
                process["name"],
                process["path"],
                self._selected_capture_mode(),
            )
            self.close()


class SteamGamePickerDialog(Gtk.Window):
    """Dialog for picking a Steam game"""

    __gsignals__ = {"game-selected": (GObject.SIGNAL_RUN_FIRST, None, (object,))}

    def __init__(self, parent):
        super().__init__()
        self.set_title(_("Select Steam game"))
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(400, 500)
        self.set_resizable(False)
        _install_game_icon_css()

        # Add ESC key handler
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        self.setup_ui()

    def setup_ui(self):
        """Build the Steam game picker UI"""
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header bar
        header = Adw.HeaderBar()
        self.set_titlebar(header)

        # Search bar
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search games…"))
        self.search_entry.set_margin_start(12)
        self.search_entry.set_margin_end(12)
        self.search_entry.set_margin_top(12)
        self.search_entry.set_margin_bottom(12)
        self.search_entry.connect("search-changed", self.on_search_changed)
        main_box.append(self.search_entry)

        # Games list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(6)
        scrolled.set_margin_end(6)

        self.games_list = Gtk.ListBox()
        self.games_list.add_css_class("boxed-list")
        self.games_list.set_margin_start(6)
        self.games_list.set_margin_end(6)
        self.games_list.set_margin_top(6)
        self.games_list.set_margin_bottom(6)
        self.games_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.games_list.connect("row-selected", self.on_game_row_selected)
        scrolled.set_child(self.games_list)

        # Store game data for later reference
        self.game_data_list = []

        # Create loading state
        self.loading_state = self.create_loading_state()

        # Store scrolled reference
        self.scrolled = scrolled

        main_box.append(scrolled)

        capture_box, self.capture_mode_dropdown = _new_capture_method_row()
        main_box.append(capture_box)

        # Add button
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        button_box.set_margin_start(12)
        button_box.set_margin_end(12)
        button_box.set_margin_top(12)
        button_box.set_margin_bottom(12)
        button_box.set_halign(Gtk.Align.CENTER)

        self.add_button = Gtk.Button(label=_("Add selected"))
        self.add_button.add_css_class("suggested-action")
        self.add_button.connect("clicked", self.on_add_clicked)
        button_box.append(self.add_button)

        main_box.append(button_box)

        self.set_child(main_box)
        self.update_add_button_state()

        # Start loading games asynchronously after UI is shown
        GLib.idle_add(self.start_loading)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events"""
        if keyval == Gdk.KEY_Escape and not state:
            self.close()
            return True
        return False

    def create_loading_state(self):
        """Create loading state widget"""
        status_page = Adw.StatusPage()
        status_page.set_title(_("Scanning Steam libraries…"))

        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        status_page.set_child(spinner)

        return status_page

    def start_loading(self):
        """Show loading state and start async game loading"""
        # Show loading state
        self.scrolled.set_child(self.loading_state)
        # Schedule populate_games to run
        GLib.idle_add(self.populate_games)
        return False  # Don't repeat idle callback

    def populate_games(self):
        """Populate with real Steam games"""
        # Try to get installed Steam games
        games = steam.get_installed_games()

        if not games:
            # Show empty state
            self.show_empty_state()
            return False  # Don't repeat idle callback

        # Convert SteamGame objects to dicts and populate list
        for game in games:
            game_data = {
                "name": game.name,
                "appid": game.appid,
                "install_path": game.install_path,
                "icon_path": game.icon_path,
            }
            self.game_data_list.append(game_data)
            row = self.create_game_row(game_data)
            self.games_list.append(row)

        # Switch to games list
        self.scrolled.set_child(self.games_list)
        self.update_add_button_state()
        return False  # Don't repeat idle callback

    def show_empty_state(self):
        """Show message when no games are found"""
        # Clear the games list and show a message
        status_page = Adw.StatusPage()
        status_page.set_icon_name(WARNING)

        steam_root = steam.find_steam_root()
        if steam_root is None:
            status_page.set_title(_("Steam not found"))
            status_page.set_description(_(
                "Could not find Steam installation.\n"
                "Make sure Steam is installed at ~/.local/share/Steam"
            ))
        else:
            status_page.set_title(_("No games found"))
            status_page.set_description(_(
                "No Steam games are currently installed.\n"
                "Install games through Steam and try again."
            ))

        # Replace the scrolled window with status page
        parent = self.scrolled
        parent.set_child(status_page)

    def create_game_row(self, game_data):
        """Create a row for a Steam game"""
        row = Gtk.ListBoxRow()

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)
        row_box.set_margin_top(12)
        row_box.set_margin_bottom(12)

        icon = _new_game_icon(game_data)
        row_box.append(icon)
        _load_game_icon_async(game_data, icon)

        # Game info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_valign(Gtk.Align.CENTER)
        info_box.set_hexpand(True)

        name_label = Gtk.Label(label=game_data["name"])
        name_label.set_halign(Gtk.Align.START)
        name_label.set_xalign(0)
        configure_single_line_ellipsis(name_label, mode=Pango.EllipsizeMode.END)
        name_label.add_css_class("heading")
        info_box.append(name_label)

        row_box.append(info_box)

        row.set_child(row_box)
        return row

    def on_search_changed(self, entry):
        """Filter games list based on search"""
        search_text = entry.get_text().lower()

        def filter_func(row):
            # Get the row index to look up game data
            index = row.get_index()
            if index < 0 or index >= len(self.game_data_list):
                return False
            game = self.game_data_list[index]
            # Search in name and appid
            return search_text in game["name"].lower() or search_text in game["appid"]

        self.games_list.set_filter_func(filter_func)
        selected_row = self.games_list.get_selected_row()
        if selected_row is not None and not filter_func(selected_row):
            self.games_list.unselect_row(selected_row)
        self.update_add_button_state()

    def on_game_row_selected(self, listbox, row):
        """Refresh the add button when a Steam game row is selected."""
        self.update_add_button_state()

    def _selected_game_data(self) -> dict | None:
        selected_row = self.games_list.get_selected_row()
        if selected_row is None:
            return None

        index = selected_row.get_index()
        if 0 <= index < len(self.game_data_list):
            return self.game_data_list[index]
        return None

    def update_add_button_state(self):
        """Enable Add Game only when a Steam game row is selected."""
        if not hasattr(self, "add_button"):
            return
        self.add_button.set_sensitive(self._selected_game_data() is not None)

    def on_add_clicked(self, button):
        """Emit signal with selected game data"""
        selected_game = self._selected_game_data()
        if selected_game is not None:
            game_data = dict(selected_game)
            game_data["capture_mode"] = dropdown_active_id(
                self.capture_mode_dropdown, DEFAULT_CAPTURE_MODE
            )
            self.emit("game-selected", game_data)
            self.close()


def show_warning_dialog(parent, message):
    """Show a simple warning dialog"""
    dialog = Adw.MessageDialog.new(parent)
    dialog.set_heading(_("Warning"))
    dialog.set_body(message)
    dialog.add_response("ok", _("OK"))
    dialog.set_default_response("ok")
    dialog.present()


def show_steam_restart_dialog(parent, on_restart):
    """Offer to restart Steam for a pending Game Capture configuration change."""
    dialog = Adw.AlertDialog.new(
        _("Game capture requires a Steam restart"),
        _("The <b>Game capture</b> method changes this game's Steam launch "
        "options. Steam must be closed while Clipper applies the change.\n\n"
        "Restart Steam now to continue, or cancel without changing the whitelist."),
    )
    dialog.set_body_use_markup(True)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("restart", _("Restart Steam"))
    dialog.set_close_response("cancel")
    dialog.set_default_response("restart")
    dialog.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

    def on_chosen(alert, result):
        if alert.choose_finish(result) == "restart":
            on_restart()

    dialog.choose(parent, None, on_chosen)
