"""Expand a gallery thumbnail into playback within the current window."""

from __future__ import annotations

import weakref
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk
from video_player import VideoPlayer


class ClipPlayerOverlay(Gtk.Widget):
    """Animate a single reusable player without changing the gallery layout."""

    OPEN_DURATION_MS = 360
    CLOSE_DURATION_MS = 280
    REVEAL_DURATION_MS = 120

    def __init__(self, *, closed_callback: Callable[[], None]) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_visible(False)
        self._closed_callback = closed_callback
        self._player = VideoPlayer(close_callback=self.dismiss)
        self._player.connect("prepared", self._on_player_prepared)
        self._player.connect("content-ready", self._on_content_ready)
        self._player.set_parent(self)
        self._animation = None
        self._reveal_animation = None
        self._reveal_progress = 0.0
        self._content_ready = False
        self._active = False
        self._closing = False
        self._progress = 0.0
        self._origin = (0.0, 0.0, 144.0, 81.0)
        self._source = None
        self._previous_focus = None
        self._poster = None
        self._closing_frame = None
        self._fullscreen_handler = None
        self._hide_handler = None
        self._fullscreen_root = None
        self._was_fullscreen = False

    def present_clip(self, clip_data, source=None) -> None:
        root = self.get_root()
        if not self._active:
            focus = root.get_focus() if isinstance(root, Gtk.Window) else None
            self._previous_focus = weakref.ref(focus) if focus is not None else None
            self._was_fullscreen = bool(root and root.is_fullscreen())
            if isinstance(root, Gtk.Window):
                self._fullscreen_root = root
                self._fullscreen_handler = root.connect(
                    "notify::fullscreened", self._player._on_fullscreen_changed
                )
                # libadwaita can reparent content (and temporarily unmap it)
                # when changing fullscreen decorations. Only a hidden host
                # ends playback; those layout changes must keep it running.
                self._hide_handler = root.connect("hide", self._on_host_hidden)
        self._pause_animation()
        self._active = True
        self._closing = False
        self._source = weakref.ref(source) if source is not None else None
        self._poster = None
        self._closing_frame = None
        self._content_ready = False
        self._reveal_progress = 0.0
        thumbnail = clip_data.get("thumbnail")
        if thumbnail:
            try:
                self._poster = Gdk.Texture.new_from_filename(str(thumbnail))
            except Exception:
                pass
        self.set_visible(True)
        self._origin = self._source_bounds()
        self._progress = 0.0
        self._player.open_clip(clip_data["path"], clip_data.get("name"), autoplay=False)
        self._player._on_fullscreen_changed()
        self._player.focus_video()

    def _on_player_prepared(self, _player) -> None:
        if self._active and not self._closing:
            # GTK/GL sink creation can be expensive on the first open. Finish
            # that work before starting the zoom's frame-clock animation.
            self._animate(1.0, self.OPEN_DURATION_MS, Adw.Easing.EASE_OUT_CUBIC)

    def _on_content_ready(self, _player) -> None:
        if self._active and not self._closing:
            self._content_ready = True
            self._reveal_player()

    def _reveal_player(self) -> None:
        if (
            not self._content_ready or self._progress < 1 or self._closing
            or self._reveal_animation is not None or self._reveal_progress >= 1
        ):
            return
        target = Adw.CallbackAnimationTarget.new(self._set_reveal_progress)
        animation = Adw.TimedAnimation.new(self, 0.0, 1.0, self.REVEAL_DURATION_MS, target)
        animation.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        animation.connect("done", self._on_reveal_done)
        self._reveal_animation = animation
        animation.play()

    def _set_reveal_progress(self, value: float, *_args) -> None:
        self._reveal_progress = value
        self.queue_draw()

    def _on_reveal_done(self, animation) -> None:
        if animation is not self._reveal_animation:
            return
        self._reveal_animation = None
        self._player.start_playback()

    def dismiss(self, *, animate: bool = True) -> None:
        if not self._active or self._closing:
            return
        self._closing = True
        paintable = self._player._picture.get_paintable()
        if paintable is not None:
            self._closing_frame = paintable.get_current_image()
        self._player.stop()
        if not self._was_fullscreen and self._player.is_fullscreen():
            self._player.unfullscreen()
        self._pause_animation()
        if not animate or not self.get_mapped():
            self._finish_close()
            return
        self._origin = self._source_bounds()
        self._animate(0.0, self.CLOSE_DURATION_MS, Adw.Easing.EASE_IN_OUT_CUBIC)

    def cleanup(self) -> None:
        if self._active:
            self._closing = True
            self._pause_animation()
            self._finish_close()
        self._player.cleanup()
        if self._player.get_parent() is self:
            self._player.unparent()

    def _source_bounds(self) -> tuple[float, float, float, float]:
        source = self._source() if self._source is not None else None
        if source is not None and source.get_mapped():
            valid, bounds = source.compute_bounds(self.get_parent())
            parent = self.get_parent()
            if (
                valid and bounds.get_width() > 0 and bounds.get_height() > 0
                and bounds.get_x() + bounds.get_width() > 0
                and bounds.get_y() + bounds.get_height() > 0
                and bounds.get_x() < parent.get_width()
                and bounds.get_y() < parent.get_height()
            ):
                return (
                    bounds.get_x(), bounds.get_y(), bounds.get_width(), bounds.get_height()
                )
        parent = self.get_parent()
        width = parent.get_width() if parent is not None else self.get_width()
        height = parent.get_height() if parent is not None else self.get_height()
        return (max(0, (width - 144) / 2), max(0, (height - 81) / 2), 144.0, 81.0)

    def _pause_animation(self) -> None:
        if self._animation is not None:
            self._animation.pause()
            self._animation = None
        if self._reveal_animation is not None:
            self._reveal_animation.pause()
            self._reveal_animation = None

    def _animate(self, target_value: float, duration: int, easing) -> None:
        target = Adw.CallbackAnimationTarget.new(self._set_progress)
        animation = Adw.TimedAnimation.new(
            self, self._progress, target_value,
            max(1, round(duration * abs(target_value - self._progress))), target,
        )
        animation.set_easing(easing)
        animation.connect("done", self._on_animation_done)
        self._animation = animation
        animation.play()

    def _set_progress(self, value: float, *_args) -> None:
        self._progress = value
        self.queue_allocate()
        self.queue_draw()

    def _on_animation_done(self, animation) -> None:
        if animation is not self._animation:
            return
        self._animation = None
        if self._closing:
            self._finish_close()
        else:
            self._player.focus_video()
            self._reveal_player()

    def _finish_close(self) -> None:
        self._active = False
        self._closing = False
        self._progress = 0.0
        self._player.stop()
        root = self._fullscreen_root
        if root is not None and not self._was_fullscreen and root.is_fullscreen():
            root.unfullscreen()
        if root is not None and self._fullscreen_handler is not None:
            root.disconnect(self._fullscreen_handler)
        if root is not None and self._hide_handler is not None:
            root.disconnect(self._hide_handler)
        self._fullscreen_handler = None
        self._hide_handler = None
        self._fullscreen_root = None
        self.set_visible(False)
        self._poster = None
        self._closing_frame = None
        self._source = None
        self._closed_callback()
        focus = self._previous_focus() if self._previous_focus is not None else None
        self._previous_focus = None
        if focus is not None and focus.get_root() is self.get_root():
            focus.grab_focus()

    def _on_host_hidden(self, *_args) -> None:
        if self._active:
            self._closing = True
            self._pause_animation()
            self._finish_close()

    def do_measure(self, _orientation, _for_size):
        # Playback must never enlarge the underlying application window.
        return 0, 0, -1, -1

    def _current_bounds(self, width: int, height: int):
        x, y, origin_width, origin_height = self._origin
        remaining = 1 - self._progress
        return (
            x * remaining, y * remaining,
            origin_width * remaining + width * self._progress,
            origin_height * remaining + height * self._progress,
        )

    def do_size_allocate(self, width, height, baseline):
        if width <= 0 or height <= 0:
            return
        x, y, current_width, current_height = self._current_bounds(width, height)
        transform = Gsk.Transform.new().translate(Graphene.Point().init(x, y)).scale(
            current_width / width, current_height / height
        )
        self._player.allocate(width, height, baseline, transform)

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        scrim = Gdk.RGBA()
        scrim.parse("black")
        scrim.alpha = 0.35 * self._progress
        snapshot.append_color(scrim, Graphene.Rect().init(0, 0, width, height))

        x, y, current_width, current_height = self._current_bounds(width, height)
        bounds = Graphene.Rect().init(x, y, current_width, current_height)
        # init_from_rect returns a borrowed alias. Retain its owning struct
        # until GTK copies it, or PyGObject can pass freed clipping bounds.
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, 12 * (1 - self._progress))
        snapshot.push_rounded_clip(rounded)
        background = Gdk.RGBA()
        background.parse("#08090a")
        snapshot.append_color(background, bounds)

        # The first frame stays paused behind the poster until both the zoom
        # and the reveal finish. Drawing it opaque behind the fading poster
        # also avoids the dark flash caused by fading both layers separately.
        if not self._closing and self._reveal_progress > 0:
            self.snapshot_child(self._player, snapshot)
        frame_opacity = self._progress if self._closing_frame is not None else 0.0
        poster_opacity = (1 - self._reveal_progress) if not self._closing else 1.0
        poster_bounds = self._preview_bounds(bounds)
        self._snapshot_frame(snapshot, self._poster, poster_bounds,
                             poster_opacity * (1 - frame_opacity))
        self._snapshot_frame(snapshot, self._closing_frame, poster_bounds, frame_opacity)
        snapshot.pop()

    def _preview_bounds(self, fallback):
        # Interpolate into the picture's actual content area, including the
        # header's space, so the handoff never moves the image at full size.
        valid, video_bounds = self._player._picture.compute_bounds(self._player)
        if not valid or video_bounds.get_width() <= 0 or video_bounds.get_height() <= 0:
            return fallback
        x, y, origin_width, origin_height = self._origin
        remaining = 1 - self._progress
        return Graphene.Rect().init(
            x * remaining + video_bounds.get_x() * self._progress,
            y * remaining + video_bounds.get_y() * self._progress,
            origin_width * remaining + video_bounds.get_width() * self._progress,
            origin_height * remaining + video_bounds.get_height() * self._progress,
        )

    @staticmethod
    def _snapshot_frame(snapshot, paintable, bounds, opacity):
        if paintable is None or opacity <= 0:
            return
        width, height = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
        if width <= 0 or height <= 0:
            return
        scale = min(bounds.get_width() / width, bounds.get_height() / height)
        width, height = width * scale, height * scale
        snapshot.save()
        snapshot.translate(Graphene.Point().init(
            bounds.get_x() + (bounds.get_width() - width) / 2,
            bounds.get_y() + (bounds.get_height() - height) / 2,
        ))
        snapshot.push_opacity(opacity)
        paintable.snapshot(snapshot, width, height)
        snapshot.pop()
        snapshot.restore()
