"""
Audio track configuration view.
"""

import copy
import os
from pathlib import Path

import gi
from i18n import _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from audio_source_discovery import list_runtime_audio_sources
from audio_source_watcher import AudioSourceWatcher
from config import AUDIO_MAX_TRACKS, normalize_audio_config
from engine_client import STATE_CONNECTED
from gi.repository import Adw, GLib, Gtk, Pango
from icon_names import ADD, TRASH
from text_helpers import configure_single_line_ellipsis, set_single_line_label_text

_MODE_VALUES = ["single_mix", "split_tracks"]
_MODE_LABELS = [_("Single mixed track"), _("Separate tracks")]


class AudioTracksView(Gtk.Box):
    """View for managing Clipper audio capture and track routing."""

    def __init__(
        self,
        config=None,
        engine_client=None,
        capabilities_requested_callback=None,
        capabilities_finished_callback=None,
        engine_restart_required_callback=None,
        engine_restart_requested_callback=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._config = config
        self._engine_client = engine_client
        self._capabilities_requested_callback = capabilities_requested_callback
        self._capabilities_finished_callback = capabilities_finished_callback
        self._engine_restart_required_callback = engine_restart_required_callback
        self._engine_restart_requested_callback = engine_restart_requested_callback
        self._suppress_signals = False
        self._source_probe_requested = False
        self._source_retry_id = None
        self._source_retry_count = 0
        self._source_refresh_timeout_id = None
        self._source_request_in_flight = False
        self._source_refresh_pending = False
        self._active = False
        self._source_watcher = AudioSourceWatcher(self._schedule_audio_source_refresh)
        self._audio = normalize_audio_config(
            self._config.get("audio") if self._config is not None else None
        )
        self._microphone_device_options = []
        self._source_options = []
        self._track_widgets = []

        if self._engine_client:
            self._engine_client.on_state_change(self._on_engine_state_change)

        self._build_source_options()
        self.setup_ui()

    def setup_ui(self):
        self._restart_banner = Adw.Banner()
        self._restart_banner.set_title(_("Engine restart required for audio changes"))
        self._restart_banner.set_button_label(_("Restart engine"))
        self._restart_banner.connect("button-clicked", self._on_restart_engine)
        self._restart_banner.set_revealed(False)
        self.append(self._restart_banner)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_margin_start(6)
        content_box.set_margin_end(6)
        content_box.set_margin_top(6)
        content_box.set_margin_bottom(6)
        content_box.set_vexpand(True)
        self.append(content_box)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        settings_box.set_margin_start(6)
        settings_box.set_margin_end(6)
        settings_box.set_margin_top(6)
        settings_box.set_margin_bottom(6)
        scrolled.set_child(settings_box)

        settings_box.append(self._create_single_mix_group())
        self._tracks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        settings_box.append(self._tracks_box)
        self._rebuild_track_groups()

        content_box.append(scrolled)
        self._load_from_config()

    def _create_single_mix_group(self):
        group = Adw.PreferencesGroup()
        group.set_title(_("Capture"))
        group.set_description(_("Choose the default audio behavior"))

        self._mode_row = Adw.ComboRow()
        self._mode_row.set_title(_("Mode"))
        self._mode_row.set_model(Gtk.StringList.new(_MODE_LABELS))
        self._mode_row.connect("notify::selected", self._on_mode_changed)
        group.add(self._mode_row)

        self._mic_row = Adw.ComboRow()
        self._mic_row.set_title(_("Microphone device"))
        self._mic_row.set_model(
            Gtk.StringList.new([option["label"] for option in self._microphone_options()])
        )
        self._mic_row.connect("notify::selected", self._on_microphone_changed)
        group.add(self._mic_row)

        self._mic_volume_row = Adw.ActionRow()
        self._mic_volume_row.set_title(_("Microphone volume"))
        self._mic_volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 200, 5
        )
        self._configure_volume_scale(self._mic_volume_scale)
        self._mic_volume_scale.set_size_request(220, -1)
        self._mic_volume_scale.set_draw_value(True)
        self._mic_volume_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self._mic_volume_scale.connect("value-changed", self._on_mic_volume_changed)
        self._mic_volume_row.add_suffix(self._mic_volume_scale)
        group.add(self._mic_volume_row)

        return group

    def _rebuild_track_groups(self):
        while child := self._tracks_box.get_first_child():
            self._tracks_box.remove(child)

        self._track_widgets = []
        group = Adw.PreferencesGroup()
        group.set_title(_("Separate tracks"))

        tracks_list = Gtk.ListBox()
        tracks_list.add_css_class("boxed-list")
        tracks_list.set_selection_mode(Gtk.SelectionMode.NONE)

        visible_indexes = self._visible_track_indexes()
        for track_index in visible_indexes:
            track = self._audio["tracks"][track_index]
            row, widgets = self._create_track_row(track)
            widgets["track_index"] = track_index
            self._track_widgets.append(widgets)
            tracks_list.append(row)

        if len(visible_indexes) < AUDIO_MAX_TRACKS:
            tracks_list.append(self._create_add_track_row())
        group.add(tracks_list)
        self._tracks_box.append(group)
        self._tracks_box.set_visible(self._audio["mode"] == "split_tracks")

    def _create_track_row(self, track):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_margin_start(12)
        row_box.set_margin_end(12)
        row_box.set_margin_top(8)
        row_box.set_margin_bottom(8)

        track_label = Gtk.Label(label=_("Track %(number)d") % {"number": track["track"]})
        track_label.set_xalign(0)
        track_label.set_width_chars(7)
        track_label.set_valign(Gtk.Align.CENTER)
        row_box.append(track_label)

        source_dropdown = Gtk.DropDown()
        source_dropdown.set_model(
            Gtk.StringList.new([option["label"] for option in self._source_options])
        )
        source_dropdown.set_factory(self._create_dropdown_label_factory())
        source_dropdown.set_list_factory(self._create_dropdown_label_factory())
        source_dropdown.set_hexpand(True)
        source_dropdown.set_valign(Gtk.Align.CENTER)
        source_dropdown.set_has_tooltip(False)
        source_dropdown.connect(
            "notify::selected",
            lambda dropdown, _unused, idx=track["track"] - 1: self._on_track_source(
                idx, dropdown.get_selected()
            ),
        )
        row_box.append(source_dropdown)

        volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        self._configure_volume_scale(volume_scale)
        volume_scale.set_size_request(180, -1)
        volume_scale.set_draw_value(True)
        volume_scale.set_value_pos(Gtk.PositionType.RIGHT)
        volume_scale.set_valign(Gtk.Align.CENTER)
        volume_scale.set_tooltip_text(
            _("Track %(number)d volume") % {"number": track["track"]}
        )
        volume_scale.connect(
            "value-changed",
            lambda scale, idx=track["track"] - 1: self._on_track_volume(
                idx, scale.get_value() / 100.0
            ),
        )
        row_box.append(volume_scale)

        remove_button = Gtk.Button.new_from_icon_name(TRASH)
        remove_button.set_valign(Gtk.Align.CENTER)
        remove_button.set_tooltip_text(
            _("Remove track %(number)d") % {"number": track["track"]}
        )
        remove_button.set_sensitive(len(self._visible_track_indexes()) > 1)
        remove_button.connect(
            "clicked",
            lambda _button, idx=track["track"] - 1: self._on_remove_track(idx),
        )
        row_box.append(remove_button)

        row.set_child(row_box)

        widgets = {
            "volume": volume_scale,
            "source": source_dropdown,
            "remove": remove_button,
        }
        return row, widgets

    def _create_add_track_row(self):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)

        button_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_content.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(ADD)
        label = Gtk.Label(label=_("Add track"))
        button_content.append(icon)
        button_content.append(label)

        add_button = Gtk.Button()
        add_button.set_child(button_content)
        add_button.set_halign(Gtk.Align.FILL)
        add_button.set_margin_start(12)
        add_button.set_margin_end(12)
        add_button.set_margin_top(8)
        add_button.set_margin_bottom(8)
        add_button.connect("clicked", self._on_add_track)

        row.set_child(add_button)
        return row

    def _create_dropdown_label_factory(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_dropdown_label)
        factory.connect("bind", self._bind_dropdown_label)
        return factory

    def _setup_dropdown_label(self, _factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0)
        configure_single_line_ellipsis(label, mode=Pango.EllipsizeMode.MIDDLE)
        list_item.set_child(label)

    def _bind_dropdown_label(self, _factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        text = item.get_string() if item is not None else ""
        set_single_line_label_text(label, text, tooltip=False)

    def _track_is_added(self, track):
        return bool(track.get("enabled") or track.get("sources"))

    def _visible_track_indexes(self):
        indexes = [
            idx
            for idx, track in enumerate(self._audio["tracks"])
            if self._track_is_added(track)
        ]
        return indexes or [0]

    def _visible_track_widget_pairs(self):
        for position, widgets in enumerate(self._track_widgets):
            track_index = widgets.get("track_index", position)
            if 0 <= track_index < len(self._audio["tracks"]):
                yield self._audio["tracks"][track_index], widgets

    def _widgets_for_track_index(self, track_index):
        for position, widgets in enumerate(self._track_widgets):
            if widgets.get("track_index", position) == track_index:
                return widgets
        return None

    def _reset_track(self, track_index):
        self._audio["tracks"][track_index] = {
            "track": track_index + 1,
            "label": f"Track {track_index + 1}",
            "enabled": False,
            "volume": 1.0,
            "sources": [],
        }

    def _assign_track(self, track_index, source_track):
        self._audio["tracks"][track_index] = {
            **copy.deepcopy(source_track),
            "track": track_index + 1,
        }

    def _compact_visible_tracks(self, tracks):
        for idx, track in enumerate(tracks):
            self._assign_track(idx, track)
            self._audio["tracks"][idx]["enabled"] = True

        for idx in range(len(tracks), AUDIO_MAX_TRACKS):
            self._reset_track(idx)

    def _load_from_config(self):
        self._suppress_signals = True
        try:
            self._mode_row.set_selected(_MODE_VALUES.index(self._audio["mode"]))
            mic = self._audio["microphone"]
            self._mic_row.set_selected(self._microphone_index_for_config())
            self._mic_volume_scale.set_value(float(mic.get("volume", 1.0)) * 100.0)

            for track, widgets in self._visible_track_widget_pairs():
                if "enabled" in widgets:
                    widgets["enabled"].set_active(bool(track.get("enabled")))
                widgets["volume"].set_value(float(track.get("volume", 1.0)) * 100.0)
                widgets["source"].set_selected(self._source_index_for_track(track))
        finally:
            self._suppress_signals = False

        self._tracks_box.set_visible(self._audio["mode"] == "split_tracks")
        self._mic_volume_row.set_visible(self._audio["mode"] == "single_mix")

    def reload_from_config(self):
        """Refresh track controls after the configuration is reset."""
        self._audio = normalize_audio_config(
            self._config.get("audio") if self._config is not None else None
        )
        self._build_source_options()
        self._rebuild_track_groups()
        self._load_from_config()
        self._restart_banner.set_revealed(False)

    def _save_audio(self, *, live_volume_update=False, reveal_restart_banner=True):
        if self._suppress_signals:
            return

        self._audio = normalize_audio_config(self._audio)
        if self._config is not None:
            self._config.set("audio", self._audio)

        if live_volume_update and (
            not self._engine_restart_required() or self._apply_live_audio_volumes()
        ):
            return

        if reveal_restart_banner and self._engine_restart_required():
            self._restart_banner.set_revealed(True)

    def _apply_live_audio_volumes(self):
        if not self._engine_client or not self._engine_client.is_connected():
            return False
        if not hasattr(self._engine_client, "update_audio_volumes"):
            return False

        return self._engine_client.update_audio_volumes(
            copy.deepcopy(self._audio), self._on_live_audio_volume_update
        )

    def _on_live_audio_volume_update(self, response):
        if response.get("ok"):
            return

        if self._engine_restart_required():
            self._restart_banner.set_revealed(True)

    def _engine_restart_required(self) -> bool:
        if self._engine_restart_required_callback is not None:
            return bool(self._engine_restart_required_callback())
        return bool(self._engine_client and self._engine_client.is_connected())

    def _on_restart_engine(self, banner):
        banner.set_revealed(False)
        if self._engine_restart_requested_callback is not None:
            self._engine_restart_requested_callback()
            return

        if self._engine_client and self._engine_client.is_connected():
            self._engine_client.set_auto_reconnect(True)
            self._engine_client.shutdown(restart=True)

    def _on_engine_state_change(self, new_state):
        self._restart_banner.set_revealed(False)
        if not getattr(self, "_active", True):
            self._source_request_in_flight = False
            return
        if new_state != STATE_CONNECTED:
            self._source_request_in_flight = False
            return

        if self._source_retry_id is not None:
            GLib.source_remove(self._source_retry_id)
            self._source_retry_id = None

        self._request_audio_sources()

    def _on_mode_changed(self, row, _unused):
        selected = row.get_selected()
        if 0 <= selected < len(_MODE_VALUES):
            self._audio["mode"] = _MODE_VALUES[selected]
            self._tracks_box.set_visible(self._audio["mode"] == "split_tracks")
            self._mic_volume_row.set_visible(self._audio["mode"] == "single_mix")
            self._save_audio()

    def _configure_volume_scale(self, scale):
        adjustment = scale.get_adjustment()
        adjustment.set_step_increment(5)
        adjustment.set_page_increment(10)

    def _on_microphone_changed(self, row, _unused):
        if self._suppress_signals:
            return

        selected = row.get_selected()
        mic = self._audio["microphone"]
        options = self._microphone_options()
        option = options[selected] if 0 <= selected < len(options) else options[0]
        mic.update(option["microphone"])
        self._save_audio()

    def _on_mic_volume_changed(self, scale):
        self._audio["microphone"]["volume"] = scale.get_value() / 100.0
        self._save_audio(live_volume_update=True)

    def _on_track_enabled(self, index, enabled):
        if index < 0 or index >= len(self._audio["tracks"]):
            return
        self._audio["tracks"][index]["enabled"] = bool(enabled)
        self._save_audio()

    def _on_track_volume(self, index, volume):
        if index < 0 or index >= len(self._audio["tracks"]):
            return
        self._audio["tracks"][index]["volume"] = volume
        self._save_audio(live_volume_update=True)

    def _on_track_source(self, index, selected):
        if self._suppress_signals:
            return

        if index < 0 or index >= len(self._audio["tracks"]):
            return

        track = self._audio["tracks"][index]
        if selected <= 0 or selected >= len(self._source_options):
            track["sources"] = []
            track["label"] = _("Track %(number)d") % {"number": index + 1}
            track.pop("source_role", None)
        else:
            option = self._source_options[selected]
            track["sources"] = [
                copy.deepcopy(source) for source in self._option_sources(option)
            ]
            track["label"] = option["label"]
            track["source_role"] = option.get("role", "custom")
            track["enabled"] = True
            widgets = self._widgets_for_track_index(index)
            if widgets is not None and "enabled" in widgets:
                widgets["enabled"].set_active(True)
        self._save_audio()

    def _on_add_track(self, _button):
        visible_indexes = self._visible_track_indexes()
        if len(visible_indexes) >= AUDIO_MAX_TRACKS:
            return

        if not any(self._track_is_added(track) for track in self._audio["tracks"]):
            self._audio["tracks"][0]["enabled"] = True
            visible_indexes = [0]

        visible_index_set = set(visible_indexes)
        next_index = next(
            (
                idx
                for idx in range(AUDIO_MAX_TRACKS)
                if idx not in visible_index_set
                and not self._track_is_added(self._audio["tracks"][idx])
            ),
            len(visible_indexes),
        )
        if next_index >= AUDIO_MAX_TRACKS:
            return

        self._reset_track(next_index)
        self._audio["tracks"][next_index]["enabled"] = True
        self._save_audio()
        self._rebuild_track_groups()
        self._load_from_config()

    def _on_remove_track(self, index):
        visible_indexes = self._visible_track_indexes()
        if len(visible_indexes) <= 1 or index not in visible_indexes:
            return

        remaining_tracks = [
            copy.deepcopy(self._audio["tracks"][track_index])
            for track_index in visible_indexes
            if track_index != index
        ]
        self._compact_visible_tracks(remaining_tracks)
        self._save_audio()
        self._rebuild_track_groups()
        self._load_from_config()

    def _microphone_options(self):
        return [
            {
                "label": _("Off"),
                "microphone": {
                    "enabled": False,
                    "backend": "pulse",
                    "device_id": "default",
                    "display_name": "System default",
                },
            },
            {
                "label": _("System default"),
                "microphone": {
                    "enabled": True,
                    "backend": "pulse",
                    "device_id": "default",
                    "display_name": "System default",
                },
            },
            *self._microphone_device_options,
        ]

    def _microphone_key(self, microphone):
        return (
            microphone.get("backend", "pulse"),
            microphone.get("device_id", "default"),
        )

    def _microphone_index_for_config(self):
        mic = self._audio["microphone"]
        if not mic.get("enabled"):
            return 0

        mic_key = self._microphone_key(mic)
        for idx, option in enumerate(self._microphone_options()):
            if option["microphone"].get("enabled") and self._microphone_key(
                option["microphone"]
            ) == mic_key:
                return idx
        return 1

    def _rebuild_microphone_model(self):
        if not hasattr(self, "_mic_row"):
            return

        self._suppress_signals = True
        try:
            self._mic_row.set_model(
                Gtk.StringList.new(
                    [option["label"] for option in self._microphone_options()]
                )
            )
            self._mic_row.set_selected(self._microphone_index_for_config())
        finally:
            self._suppress_signals = False

    def _microphone_options_signature(self):
        return tuple(
            (
                option["label"],
                option["microphone"].get("enabled", False),
                option["microphone"].get("backend", "pulse"),
                option["microphone"].get("device_id", "default"),
            )
            for option in self._microphone_options()
        )

    def _build_microphone_device_options(self, discovered_sources):
        options = []
        seen = {("pulse", "default")}

        for item in discovered_sources or []:
            if not isinstance(item, dict) or item.get("kind") != "input_device":
                continue

            backend = item.get("backend") or "pulse"
            device_id = item.get("device_id") or item.get("id") or "default"
            key = (backend, device_id)
            if key in seen:
                continue
            seen.add(key)

            display_name = item.get("display_name") or device_id
            options.append(
                {
                    "label": display_name,
                    "microphone": {
                        "enabled": True,
                        "backend": backend,
                        "device_id": device_id,
                        "display_name": display_name,
                    },
                }
            )

        mic = self._audio["microphone"]
        mic_key = self._microphone_key(mic)
        if mic.get("enabled") and mic_key not in seen and mic_key != ("pulse", "default"):
            options.append(
                {
                    "label": mic.get("display_name", mic.get("device_id", "Microphone")),
                    "microphone": {
                        "enabled": True,
                        "backend": mic.get("backend", "pulse"),
                        "device_id": mic.get("device_id", "default"),
                        "display_name": mic.get("display_name", "Microphone"),
                    },
                }
            )

        self._microphone_device_options = options

    def _selected_microphone_source(self):
        return {
            "kind": "selected_input_device",
            "display_name": "Microphone",
        }

    def _system_audio_source(self):
        return {
            "kind": "output_device",
            "backend": "pulse",
            "device_id": "default",
            "display_name": "System Audio",
        }

    def _build_source_options(self, discovered_sources=None):
        self._build_microphone_device_options(discovered_sources)
        self._source_options = [
            {"label": _("None"), "role": "none", "source": None},
            {
                "label": _("System audio"),
                "role": "system_audio",
                "source": self._system_audio_source(),
            },
            {
                "label": _("Microphone"),
                "role": "microphone",
                "source": self._selected_microphone_source(),
            },
        ]

        self._source_options.append(
            {
                "label": _("Whitelisted games"),
                "role": "whitelisted_games",
                "sources": self._whitelist_game_sources(),
            }
        )

        for item in discovered_sources or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            display_name = item.get("display_name") or item.get("app_name") or item.get("id")
            media_name = item.get("media_name")
            if kind == "application":
                label = _("App: %(name)s") % {"name": display_name}
                if media_name and media_name != display_name:
                    label = f"{label} - {media_name}"
                self._source_options.append(
                    {
                        "label": label,
                        "role": "application",
                        "source": {
                            "kind": "application",
                            "display_name": display_name,
                            "match": {
                                "type": "pipewire_app",
                                "value": item.get("binary")
                                or item.get("app_name")
                                or display_name,
                                "priority": "binary_first",
                            },
                        },
                    }
                )

        self._append_configured_source_options()

    def _whitelist_game_sources(self):
        if self._config is None:
            return []

        sources = []
        seen = set()
        for entry in self._config.get("whitelist", []):
            if not isinstance(entry, dict):
                continue

            configured_source = self._configured_whitelist_audio_source(entry)
            inferred_match_value = self._whitelist_audio_match_value(entry)
            if configured_source and not self._inferred_match_is_better(
                inferred_match_value, configured_source
            ):
                source = configured_source
            else:
                source = self._whitelist_source_from_match(entry, inferred_match_value)

            if not source:
                continue

            key = self._source_key(source)
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)

        return sources

    def _whitelist_source_from_match(self, entry, match_value):
        if not match_value:
            return None

        name = entry.get("name") or match_value
        source = {
            "kind": "game_app",
            "display_name": name,
            "match": {
                "type": "process_name",
                "value": match_value,
                "priority": "binary_first",
            },
        }

        learned_from = {}
        if entry.get("appid"):
            learned_from["steam_appid"] = str(entry["appid"])
        if entry.get("install_path"):
            learned_from["install_path"] = str(entry["install_path"])
        if learned_from:
            source["learned_from"] = learned_from

        return source

    def _configured_whitelist_audio_source(self, entry):
        appid = str(entry.get("appid") or "")
        install_path = str(entry.get("install_path") or "")
        entry_terms = self._whitelist_match_terms(entry)

        for track in self._audio["tracks"]:
            for source in track.get("sources", []):
                if not isinstance(source, dict) or source.get("kind") != "game_app":
                    continue

                learned_from = source.get("learned_from", {})
                if isinstance(learned_from, dict):
                    if appid and str(learned_from.get("steam_appid") or "") == appid:
                        return copy.deepcopy(source)
                    if (
                        install_path
                        and str(learned_from.get("install_path") or "") == install_path
                    ):
                        return copy.deepcopy(source)

                source_terms = self._runtime_source_terms(source)
                if any(
                    self._normalized_term_matches(entry_term, source_term)
                    for entry_term in entry_terms
                    for source_term in source_terms
                ):
                    return copy.deepcopy(source)

        return None

    def _inferred_match_is_better(self, inferred_match_value, configured_source):
        if not inferred_match_value:
            return False

        configured_match = configured_source.get("match", {})
        configured_value = configured_match.get("value", "")
        if not isinstance(configured_value, str) or not configured_value.strip():
            return True

        if self._normalize_match_term(inferred_match_value) == self._normalize_match_term(
            configured_value
        ):
            return False

        return self._looks_like_game_executable(inferred_match_value) and not (
            self._looks_like_game_executable(configured_value)
        )

    def _looks_like_game_executable(self, value):
        if not isinstance(value, str):
            return False
        return value.lower().endswith((".exe", ".x86_64", ".x86", ".appimage"))

    def _whitelist_audio_match_value(self, entry):
        for key in (
            "audio_process",
            "audio_binary",
            "executable_name",
            "executable_path",
            "path",
        ):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if "/" in value:
                value = Path(value).name
            if value:
                return value

        install_match = self._install_path_audio_match_value(
            entry.get("install_path"), entry.get("name")
        )
        if install_match:
            return install_match

        value = entry.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    def _install_path_audio_match_value(self, install_path, game_name):
        if not isinstance(install_path, str) or not install_path.strip():
            return ""

        path = Path(install_path)
        if not path.is_dir():
            return path.name

        best_name = ""
        best_score = -1
        for child in path.iterdir():
            if not child.is_file():
                continue

            name = child.name
            if not self._looks_like_game_executable(name) and not os.access(child, os.X_OK):
                continue
            if self._should_ignore_game_executable(name):
                continue

            score = self._game_executable_score(name, path.name, game_name)
            if score > best_score:
                best_name = name
                best_score = score

        return best_name or path.name

    def _should_ignore_game_executable(self, name):
        lowered = name.lower()
        ignored_markers = (
            "crashhandler",
            "unitycrashhandler",
            "installer",
            "redist",
            "setup",
            "unins",
            "vc_redist",
        )
        ignored_suffixes = (".dll", ".so", ".dylib")
        return lowered.endswith(ignored_suffixes) or any(
            marker in lowered for marker in ignored_markers
        )

    def _game_executable_score(self, executable_name, install_dir_name, game_name):
        executable_stem = self._normalize_executable_stem(executable_name)
        install_dir = self._normalize_match_term(install_dir_name)
        game = self._normalize_match_term(game_name)

        score = 0
        if executable_stem and executable_stem == install_dir:
            score += 100
        elif executable_stem and (
            executable_stem in install_dir or install_dir in executable_stem
        ):
            score += 70

        if executable_stem and executable_stem == game:
            score += 90
        elif executable_stem and game and (executable_stem in game or game in executable_stem):
            score += 60

        if self._looks_like_game_executable(executable_name):
            score += 10

        return score

    def _normalize_executable_stem(self, value):
        if not isinstance(value, str):
            return ""

        lowered = value.lower()
        for suffix in (".x86_64", ".appimage", ".exe", ".x86"):
            if lowered.endswith(suffix):
                value = value[: -len(suffix)]
                break
        return self._normalize_match_term(value)

    def _whitelist_match_terms(self, entry):
        terms = []
        for key in (
            "audio_process",
            "audio_binary",
            "executable_name",
            "executable_path",
            "path",
            "install_path",
            "name",
        ):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if "/" in value:
                terms.append(Path(value).name)
            terms.append(value)
        return [term for term in terms if term]

    def _runtime_source_terms(self, source):
        return [
            value
            for value in (
                source.get("binary"),
                source.get("app_name"),
                source.get("display_name"),
                source.get("media_name"),
            )
            if isinstance(value, str) and value.strip()
        ]

    def _normalized_term_matches(self, left, right):
        left = self._normalize_match_term(left)
        right = self._normalize_match_term(right)
        if not left or not right:
            return False
        return left == right or left in right or right in left

    def _normalize_match_term(self, value):
        if not isinstance(value, str):
            return ""
        value = Path(value.strip()).name.lower()
        return "".join(ch for ch in value if ch.isalnum())

    def _append_configured_source_options(self):
        for track in self._audio["tracks"]:
            sources = [
                copy.deepcopy(source)
                for source in track.get("sources", [])
                if isinstance(source, dict)
            ]
            if not sources:
                continue

            if any(
                self._source_list_matches(self._option_sources(option), sources)
                for option in self._source_options
            ):
                continue

            label = track.get("label") or self._label_for_sources(sources)

            self._source_options.append(
                {
                    "label": label,
                    "sources": sources,
                }
            )

    def _label_for_sources(self, sources):
        if len(sources) > 1 and all(source.get("kind") == "game_app" for source in sources):
            return _("Whitelisted games")

        source = sources[0]
        label = source.get("display_name")
        if not label:
            label = source.get("match", {}).get("value", "Application audio")
        if source.get("kind") == "game_app":
            label = _("Game: %(name)s") % {"name": label}
        elif source.get("kind") == "application":
            label = _("App: %(name)s") % {"name": label}
        return label

    def _option_sources(self, option):
        if "sources" in option:
            return option["sources"]
        source = option.get("source")
        return [] if source is None else [source]

    def _source_matches(self, option_source, source):
        if option_source is None or source is None:
            return option_source is source
        return self._source_key(option_source) == self._source_key(source)

    def _source_key(self, source):
        if not isinstance(source, dict):
            return ("none",)

        kind = source.get("kind")
        if kind in ("input_device", "output_device"):
            return (
                kind,
                source.get("backend", "pulse"),
                source.get("device_id", "default"),
            )
        match = source.get("match", {})
        return (
            kind,
            match.get("type", ""),
            match.get("value", ""),
        )

    def _source_list_matches(self, option_sources, track_sources):
        if len(option_sources) != len(track_sources):
            return False
        return all(
            self._source_matches(option_source, track_source)
            for option_source, track_source in zip(option_sources, track_sources, strict=True)
        )

    def _source_index_for_track(self, track):
        # An empty whitelist intentionally produces an aggregate option with no
        # concrete sources yet.  Preserve that semantic selection instead of
        # treating its empty source list as the "None" option.
        if track.get("source_role") == "whitelisted_games" or (
            not track.get("source_role") and track.get("label") == "Whitelisted games"
        ):
            for idx, option in enumerate(self._source_options):
                if option.get("role") == "whitelisted_games":
                    return idx

        sources = track.get("sources") or []
        if not sources:
            return 0

        for idx, option in enumerate(self._source_options):
            if self._source_list_matches(self._option_sources(option), sources):
                return idx
        return 0

    def _rebuild_source_models(self):
        self._suppress_signals = True
        try:
            labels = [option["label"] for option in self._source_options]
            for track, widgets in self._visible_track_widget_pairs():
                widgets["source"].set_model(Gtk.StringList.new(labels))
                widgets["source"].set_selected(self._source_index_for_track(track))
        finally:
            self._suppress_signals = False

    def _sync_whitelisted_game_tracks(self):
        whitelist_option = next(
            (
                option
                for option in self._source_options
                if option.get("role") == "whitelisted_games"
            ),
            None,
        )
        if whitelist_option is None:
            return False

        whitelist_sources = self._option_sources(whitelist_option)
        changed = False
        for track in self._audio["tracks"]:
            sources = track.get("sources") or []
            if not (
                track.get("source_role") == "whitelisted_games"
                or (
                    not track.get("source_role")
                    and track.get("label") == "Whitelisted games"
                )
            ) and not (
                len(sources) > 1
                and all(source.get("kind") == "game_app" for source in sources)
            ):
                continue
            if self._source_list_matches(whitelist_sources, sources):
                continue

            track["label"] = _("Whitelisted games")
            track["source_role"] = "whitelisted_games"
            track["sources"] = [copy.deepcopy(source) for source in whitelist_sources]
            if sources or whitelist_sources:
                changed = True

        return changed

    def on_whitelist_changed(self):
        previous_signature = self._source_options_signature()
        self._build_source_options()
        tracks_changed = self._sync_whitelisted_game_tracks()
        if self._source_options_signature() != previous_signature:
            self._rebuild_source_models()
        if tracks_changed:
            self._save_audio(reveal_restart_banner=False)

    def cleanup(self) -> None:
        """Stop the pactl watcher and outstanding refresh timers."""
        self._active = False
        self._source_watcher.stop()
        for attribute in ("_source_retry_id", "_source_refresh_timeout_id"):
            source_id = getattr(self, attribute, None)
            if source_id is not None:
                GLib.source_remove(source_id)
                setattr(self, attribute, None)
        self._finish_source_probe()

    def set_active(self, active: bool) -> None:
        """Run source discovery only while the Audio page is visible."""
        active = bool(active)
        if active == self._active:
            return

        self._active = active
        if active:
            self._source_watcher.start()
            self._request_audio_capabilities()
            return

        self._source_watcher.stop()
        if self._source_refresh_timeout_id is not None:
            GLib.source_remove(self._source_refresh_timeout_id)
            self._source_refresh_timeout_id = None
        if self._source_retry_id is not None:
            GLib.source_remove(self._source_retry_id)
            self._source_retry_id = None
        self._source_refresh_pending = False
        self._finish_source_probe()

    def _source_options_signature(self):
        return tuple(
            (
                option["label"],
                tuple(self._source_key(source) for source in self._option_sources(option)),
            )
            for option in self._source_options
        )

    def _schedule_audio_source_refresh(self):
        if not self._active:
            return
        if self._source_refresh_timeout_id is None:
            self._source_refresh_timeout_id = GLib.timeout_add(
                350, self._run_scheduled_audio_source_refresh
            )

    def _run_scheduled_audio_source_refresh(self):
        self._source_refresh_timeout_id = None
        self._request_audio_sources()
        return False

    def _request_audio_capabilities(self):
        if self._engine_client and self._engine_client.is_connected():
            self._engine_client.get_audio_capabilities(lambda _response: None)
            self._request_audio_sources()
            return

        self._ensure_engine_for_audio_sources()

    def _request_audio_sources(self):
        if not self._engine_client or not self._engine_client.is_connected():
            self._on_audio_sources(
                {"ok": True, "sources": list_runtime_audio_sources()},
                finish_probe=False,
            )
            return

        if self._source_request_in_flight:
            self._source_refresh_pending = True
            return

        self._source_request_in_flight = True
        if not self._engine_client.list_audio_sources(self._on_audio_sources):
            self._source_request_in_flight = False

    def _ensure_engine_for_audio_sources(self):
        if self._source_probe_requested:
            return

        if self._capabilities_requested_callback is None:
            return

        self._source_probe_requested = True
        self._source_retry_count = 0
        if not self._capabilities_requested_callback():
            self._source_probe_requested = False
            return

        if self._source_retry_id is None:
            self._source_retry_id = GLib.timeout_add(250, self._retry_audio_sources)

    def _retry_audio_sources(self):
        self._source_retry_count += 1
        if self._engine_client and self._engine_client.is_connected():
            self._source_retry_id = None
            self._request_audio_sources()
            return False

        if self._source_retry_count >= 40:
            self._source_retry_id = None
            self._finish_source_probe()
            return False

        return True

    def _finish_source_probe(self):
        if not getattr(self, "_source_probe_requested", False):
            return

        self._source_probe_requested = False
        if self._capabilities_finished_callback is not None:
            self._capabilities_finished_callback()

    def _on_audio_sources(self, response, finish_probe=True):
        self._source_request_in_flight = False
        if finish_probe:
            self._finish_source_probe()

        if self._source_refresh_pending:
            self._source_refresh_pending = False
            self._schedule_audio_source_refresh()

        if not response.get("ok"):
            return

        previous_signature = self._source_options_signature()
        previous_microphone_signature = self._microphone_options_signature()
        self._build_source_options(response.get("sources", []))
        tracks_changed = self._sync_whitelisted_game_tracks()
        if self._microphone_options_signature() != previous_microphone_signature:
            self._rebuild_microphone_model()
        if self._source_options_signature() != previous_signature:
            self._rebuild_source_models()
        if tracks_changed:
            self._save_audio(reveal_restart_banner=False)
