"""GTK presentation for Clipper's small fruit-drop easter egg."""

from __future__ import annotations

from collections.abc import Callable

import gi
from i18n import _

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk
from suika_game_model import (
    BOARD_HEIGHT,
    BOARD_INSET,
    BOARD_WIDTH,
    DANGER_LINE_Y,
    FRUIT_RADII,
    SPAWN_Y,
    Fruit,
    FruitDropGame,
)

_TICK_INTERVAL_MS = 16
_FRUIT_COLORS = (
    (0.89, 0.18, 0.28),
    (0.98, 0.32, 0.28),
    (0.55, 0.30, 0.76),
    (0.96, 0.49, 0.10),
    (0.99, 0.67, 0.08),
    (0.88, 0.25, 0.23),
    (0.42, 0.74, 0.20),
    (0.98, 0.42, 0.60),
    (0.94, 0.77, 0.12),
    (0.16, 0.65, 0.61),
    (0.10, 0.43, 0.31),
)


class SuikaGameWindow(Adw.Window):
    """A compact, self-contained fruit-drop game window."""

    def __init__(
        self,
        *,
        transient_for,
        high_score: int,
        high_score_changed: Callable[[int], None],
    ) -> None:
        super().__init__(title=_("Fruit drop"), transient_for=transient_for, modal=True)
        self.set_default_size(390, 610)
        self.set_size_request(350, 500)
        self.set_resizable(False)
        self.add_css_class("fruit-drop-window")
        self._install_styles()
        self._game = FruitDropGame()
        self._high_score = max(0, high_score)
        self._high_score_changed = high_score_changed
        self._pointer_x = BOARD_WIDTH / 2
        self._closed = False
        self._style_manager = Adw.StyleManager.get_default()
        self._theme_handler_id = 0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(label=_("Fruit drop"))
        title.add_css_class("title")
        header.set_title_widget(title)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        scoreboard = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scoreboard.set_halign(Gtk.Align.CENTER)
        self._score_value = self._score_card(scoreboard, _("Score"), 0)
        self._best_value = self._score_card(scoreboard, _("Best"), self._high_score)
        content.append(scoreboard)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        self._board = Gtk.DrawingArea()
        self._board.set_content_width(int(BOARD_WIDTH))
        self._board.set_content_height(int(BOARD_HEIGHT))
        self._board.set_hexpand(True)
        self._board.set_vexpand(True)
        self._board.set_draw_func(self._draw_board)
        self._board.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Fruit drop game board")]
        )
        overlay.set_child(self._board)
        self._game_over_box = self._make_game_over_overlay()
        overlay.add_overlay(self._game_over_box)
        content.append(overlay)

        toolbar.set_content(content)
        self.set_content(toolbar)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_board_pressed)
        self._board.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_board_motion)
        motion.connect("leave", self._on_board_leave)
        self._board.add_controller(motion)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        self._theme_handler_id = self._style_manager.connect(
            "notify::dark", self._on_theme_changed
        )
        self._tick_id = GLib.timeout_add(_TICK_INTERVAL_MS, self._on_tick)

    @staticmethod
    def _install_styles() -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            .fruit-drop-score {
                background-color: @card_bg_color;
                border: 1px solid @borders;
                border-radius: 12px;
                padding: 8px 16px;
            }

            .fruit-drop-game-over {
                background-color: @dialog_bg_color;
                background-image: none;
                border: 1px solid @borders;
                border-radius: 16px;
                box-shadow: 0 8px 24px alpha(black, 0.35);
                padding: 20px 28px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    @staticmethod
    def _score_card(parent, label: str, value: int):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        card.add_css_class("fruit-drop-score")
        card.set_size_request(94, -1)
        heading = Gtk.Label(label=label)
        heading.add_css_class("caption")
        score = Gtk.Label(label=str(value))
        score.add_css_class("title-2")
        card.append(heading)
        card.append(score)
        parent.append(card)
        return score

    def _make_game_over_overlay(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("fruit-drop-game-over")
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(32)
        box.set_margin_end(32)
        title = Gtk.Label(label=_("Game over"))
        title.add_css_class("title-1")
        box.append(title)
        again_button = Gtk.Button(label=_("Play again"))
        again_button.add_css_class("suggested-action")
        again_button.connect("clicked", self._on_new_game)
        box.append(again_button)
        box.set_visible(False)
        return box

    def _on_board_pressed(self, _gesture, _count, x: float, _y: float) -> None:
        self._pointer_x = self._board_x(x)
        self._game.drop(self._pointer_x)
        self._board.queue_draw()

    def _on_board_motion(self, _motion, x: float, _y: float) -> None:
        self._pointer_x = self._board_x(x)
        self._board.queue_draw()

    def _on_board_leave(self, _motion) -> None:
        self._pointer_x = BOARD_WIDTH / 2
        self._board.queue_draw()

    def _on_new_game(self, _button) -> None:
        self._game.reset()
        self._game_over_box.set_visible(False)
        self._score_value.set_label("0")
        self._board.queue_draw()

    def _on_tick(self) -> bool:
        if self._closed:
            return False
        was_over = self._game.game_over
        self._game.advance(_TICK_INTERVAL_MS / 1000)
        if self._game.score > self._high_score:
            self._high_score = self._game.score
            self._best_value.set_label(str(self._high_score))
            self._high_score_changed(self._high_score)
        self._score_value.set_label(str(self._game.score))
        if self._game.game_over and not was_over:
            self._game_over_box.set_visible(True)
        self._board.queue_draw()
        return True

    def _on_close_request(self, _window) -> bool:
        self._stop_game_loop()
        return False

    def _on_destroy(self, _window) -> None:
        self._stop_game_loop()
        if self._theme_handler_id:
            self._style_manager.disconnect(self._theme_handler_id)
            self._theme_handler_id = 0

    def _on_theme_changed(self, _style_manager, _property) -> None:
        self._board.queue_draw()

    def _stop_game_loop(self) -> None:
        self._closed = True
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _board_x(self, widget_x: float) -> float:
        width = self._board.get_width()
        height = self._board.get_height()
        scale = min(width / BOARD_WIDTH, height / BOARD_HEIGHT) if width and height else 1.0
        offset_x = (width - BOARD_WIDTH * scale) / 2
        return (widget_x - offset_x) / scale

    def _draw_board(self, _area, context, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return
        scale = min(width / BOARD_WIDTH, height / BOARD_HEIGHT)
        offset_x = (width - BOARD_WIDTH * scale) / 2
        offset_y = (height - BOARD_HEIGHT * scale) / 2
        context.save()
        context.translate(offset_x, offset_y)
        context.scale(scale, scale)

        palette = self._board_palette()
        context.set_source_rgb(*palette["canvas"])
        context.paint()
        self._rounded_rectangle(context, 6, 6, BOARD_WIDTH - 12, BOARD_HEIGHT - 12, 20)
        context.set_source_rgb(*palette["frame"])
        context.fill()
        self._rounded_rectangle(context, 10, 10, BOARD_WIDTH - 20, BOARD_HEIGHT - 20, 16)
        context.set_source_rgb(*palette["garden"])
        context.fill()
        self._draw_garden_stripes(context, palette)

        context.set_dash((5.0, 5.0), 0)
        context.set_source_rgba(*palette["danger"], 0.85)
        context.set_line_width(2)
        context.move_to(BOARD_INSET + 4, DANGER_LINE_Y)
        context.line_to(BOARD_WIDTH - BOARD_INSET - 4, DANGER_LINE_Y)
        context.stroke()
        context.set_dash((), 0)

        if self._game.can_drop:
            self._draw_fruit(
                context,
                Fruit(self._game.next_kind, self._clamp_preview_x(), SPAWN_Y),
                alpha=0.42,
            )
        for fruit in self._game.fruits:
            self._draw_fruit(context, fruit)
        context.restore()

    @staticmethod
    def _rounded_rectangle(context, x: float, y: float, width: float, height: float, radius: float):
        context.new_sub_path()
        context.arc(x + width - radius, y + radius, radius, -1.5708, 0)
        context.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
        context.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
        context.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
        context.close_path()

    @staticmethod
    def _draw_garden_stripes(context, palette) -> None:
        inner_left = 10.0
        inner_top = 10.0
        inner_width = BOARD_WIDTH - 20.0
        inner_height = BOARD_HEIGHT - 20.0
        inner_bottom = inner_top + inner_height
        context.save()
        SuikaGameWindow._rounded_rectangle(
            context, inner_left, inner_top, inner_width, inner_height, 16
        )
        context.clip()

        grass_height = 28.0
        hump_count = 7
        hump_width = inner_width / hump_count
        grass_top = inner_bottom - grass_height
        context.set_source_rgb(*palette["ground"])
        context.move_to(inner_left, inner_bottom)
        context.line_to(inner_left, grass_top)
        for index in range(hump_count):
            hump_left = inner_left + index * hump_width
            context.curve_to(
                hump_left + hump_width * 0.25,
                grass_top - grass_height * 0.62,
                hump_left + hump_width * 0.75,
                grass_top - grass_height * 0.62,
                hump_left + hump_width,
                grass_top,
            )
        context.line_to(inner_left + inner_width, inner_bottom)
        context.close_path()
        context.fill()

        context.set_source_rgba(*palette["highlight"], 0.18)
        context.arc(75, 115, 42, 0, 6.2832)
        context.fill()
        context.arc(245, 165, 55, 0, 6.2832)
        context.fill()
        context.restore()

    def _board_palette(self) -> dict[str, tuple[float, float, float]]:
        if self._style_manager.get_dark():
            return {
                "canvas": (0.12, 0.13, 0.12),
                "frame": (0.18, 0.37, 0.25),
                "garden": (0.17, 0.24, 0.17),
                "ground": (0.30, 0.50, 0.25),
                "highlight": (0.86, 0.94, 0.76),
                "danger": (0.94, 0.38, 0.40),
            }
        return {
            "canvas": (0.98, 0.96, 0.88),
            "frame": (0.25, 0.42, 0.29),
            "garden": (0.91, 0.96, 0.79),
            "ground": (0.55, 0.78, 0.36),
            "highlight": (1.0, 1.0, 1.0),
            "danger": (0.76, 0.18, 0.18),
        }

    def _clamp_preview_x(self) -> float:
        radius = FRUIT_RADII[self._game.next_kind]
        return min(
            BOARD_WIDTH - BOARD_INSET - radius,
            max(BOARD_INSET + radius, self._pointer_x),
        )

    @staticmethod
    def _draw_fruit(context, fruit: Fruit, *, alpha: float = 1.0) -> None:
        red, green, blue = _FRUIT_COLORS[fruit.kind]
        radius = fruit.radius
        context.set_source_rgba(red * 0.62, green * 0.62, blue * 0.62, alpha)
        context.arc(fruit.x + 2, fruit.y + 3, radius, 0, 6.2832)
        context.fill()
        context.set_source_rgba(red, green, blue, alpha)
        context.arc(fruit.x, fruit.y, radius, 0, 6.2832)
        context.fill_preserve()
        context.set_source_rgba(0.14, 0.23, 0.12, alpha * 0.48)
        context.set_line_width(max(1.2, radius * 0.06))
        context.stroke()
        context.set_source_rgba(1.0, 1.0, 1.0, alpha * 0.38)
        context.arc(fruit.x - radius * 0.31, fruit.y - radius * 0.30, radius * 0.22, 0, 6.2832)
        context.fill()
        if fruit.kind >= 2:
            context.set_source_rgba(0.20, 0.43, 0.18, alpha)
            context.move_to(fruit.x, fruit.y - radius * 0.82)
            context.line_to(fruit.x + radius * 0.36, fruit.y - radius * 1.04)
            context.line_to(fruit.x + radius * 0.22, fruit.y - radius * 0.62)
            context.close_path()
            context.fill()
