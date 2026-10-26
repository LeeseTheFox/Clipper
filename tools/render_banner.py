"""Compose README banners inside the screenshot generator's pinned SDK."""

from __future__ import annotations

import math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def render_banner(output: Path, font_family: str) -> None:
    """Arrange all four captures on a rounded canvas using libadwaita's palette."""
    import cairo
    import gi

    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Rsvg", "2.0")
    from gi.repository import Adw, Gtk, Rsvg

    Adw.init()
    style = Adw.StyleManager.get_default()
    style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    widget = Gtk.Label()

    def theme_color(name):
        found, color = widget.get_style_context().lookup_color(name)
        if not found:
            raise RuntimeError(f"Missing GTK banner color: {name}")
        return color.red, color.green, color.blue

    background = theme_color("window_bg_color")
    foreground = theme_color("window_fg_color")
    accent = theme_color("accent_bg_color")
    shade = theme_color("shade_color")

    def mix(first, second, amount):
        return tuple(a * (1 - amount) + b * amount for a, b in zip(first, second, strict=True))

    scenes = ("games", "clips", "editor", "audio")
    images = {
        scene: cairo.ImageSurface.create_from_png(str(output / f"{scene}-dark.png"))
        for scene in scenes
    }
    window_size = (images["games"].get_width(), images["games"].get_height())
    for scene, image in images.items():
        if (image.get_width(), image.get_height()) != window_size:
            raise RuntimeError(f"Banner screenshot must match the main window size: {scene}")
    x, y, width = 480, 80, 1240
    height = width * window_size[1] / window_size[0]
    canvas_height = round(height + 2 * y)
    brand_y = (canvas_height - 484) / 2
    canvas_width = 1800
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_width, canvas_height)
    context = cairo.Context(surface)

    def rounded(x, y, width, height, radius):
        context.new_sub_path()
        for cx, cy, angle in (
            (x + width - radius, y + radius, -90),
            (x + width - radius, y + height - radius, 0),
            (x + radius, y + height - radius, 90),
            (x + radius, y + radius, 180),
        ):
            context.arc(cx, cy, radius, math.radians(angle), math.radians(angle + 90))
        context.close_path()

    def polygon(points, offset_x=0, offset_y=0):
        context.move_to(offset_x + points[0][0], offset_y + points[0][1])
        for px, py in points[1:]:
            context.line_to(offset_x + px, offset_y + py)
        context.close_path()

    def shadow(x, y, width, height, radius, blur, offset, opacity, *, silhouette=None):
        """Blur one silhouette rather than accumulating hard-edged outlines."""
        padding = math.ceil(3 * blur)
        full_width, full_height = width + 2 * padding, height + 2 * padding
        color = ",".join(str(round(channel * 255)) for channel in shade)
        if silhouette is None:
            shape = f'''<rect x="{padding}" y="{padding}" width="{width}" height="{height}"
                rx="{radius}" fill="rgb({color})" fill-opacity="{opacity}"
                filter="url(#shadow)"/>'''
        else:
            shape = f'''<g transform="translate({padding},{padding})"
                fill="rgb({color})" fill-opacity="{opacity}" filter="url(#shadow)">
                {silhouette}</g>'''
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="{full_width}" height="{full_height}">
          <defs><filter id="shadow" x="-100%" y="-100%" width="300%" height="300%">
            <feGaussianBlur stdDeviation="{blur}"/>
          </filter></defs>
          {shape}
        </svg>'''
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y = x - padding, y - padding + offset
        viewport.width, viewport.height = full_width, full_height
        Rsvg.Handle.new_from_data(svg.encode()).render_document(context, viewport)

    # Leave the corners transparent so the silhouette survives any README theme.
    rounded(0, 0, canvas_width, canvas_height, 40)
    context.clip()
    gradient = cairo.LinearGradient(0, 0, canvas_width, canvas_height)
    gradient.add_color_stop_rgb(0, *mix(background, accent, 0.18))
    gradient.add_color_stop_rgb(0.5, *mix(background, accent, 0.1))
    gradient.add_color_stop_rgb(1, *mix(background, accent, 0.36))
    context.set_source(gradient)
    context.paint()

    glow = cairo.RadialGradient(240, canvas_height * 0.4, 30, 240, canvas_height * 0.4, 740)
    glow.add_color_stop_rgba(0, *accent, 0.18)
    glow.add_color_stop_rgba(1, *accent, 0)
    context.set_source(glow)
    context.paint()
    # One translucent diagonal plane gives the color field depth without busy lines.
    context.move_to(620, 0)
    context.line_to(1030, 0)
    context.line_to(1450, canvas_height)
    context.line_to(1040, canvas_height)
    context.close_path()
    plane = cairo.LinearGradient(620, 0, 1450, canvas_height)
    plane.add_color_stop_rgba(0, *foreground, 0.045)
    plane.add_color_stop_rgba(1, *foreground, 0.015)
    context.set_source(plane)
    context.fill()

    # Let the original icon float above the darker field, with no extra outline.
    shadow(70, brand_y, 324, 324, 54, 30, 16, 0.38)
    icon = Rsvg.Handle.new_from_file(
        str(REPO / "ui/icons/hicolor/scalable/apps/io.github.leesethefox.Clipper.svg")
    )
    viewport = Rsvg.Rectangle()
    viewport.x, viewport.y, viewport.width, viewport.height = 70, brand_y, 324, 324
    icon.render_document(context, viewport)
    context.select_font_face(font_family, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    context.set_font_size(80)
    title_x, title_y = 78, brand_y + 442
    for blur_radius in (2, 4, 6):
        for step in range(8):
            angle = step * math.tau / 8
            context.set_source_rgba(*shade, 0.025)
            context.move_to(
                title_x + 1 + math.cos(angle) * blur_radius,
                title_y + 5 + math.sin(angle) * blur_radius,
            )
            context.show_text("Clipper")
    context.set_source_rgb(*foreground)
    context.move_to(title_x, title_y)
    context.show_text("Clipper")
    # Span the ink of "Cli" and align the bottom with the p's descender.
    prefix = context.text_extents("Cli")
    descender = context.text_extents("p")
    accent_bottom = title_y + descender.y_bearing + descender.height
    accent_height = 7
    rounded(
        title_x + prefix.x_bearing,
        accent_bottom - accent_height,
        prefix.width,
        accent_height,
        accent_height / 2,
    )
    context.set_source_rgb(*accent)
    context.fill()
    # Start under the right half of the second p, clear of its descender stem,
    # and finish at the visible edge of the r.
    second_p_x = title_x + context.text_extents("Clip").x_advance
    suffix_start = second_p_x + descender.x_bearing + descender.width / 2
    title = context.text_extents("Clipper")
    suffix_end = title_x + title.x_bearing + title.width
    rounded(
        suffix_start,
        accent_bottom - accent_height,
        suffix_end - suffix_start,
        accent_height,
        accent_height / 2,
    )
    context.fill()

    radius = 18
    # Corner sections look heavier because of their broad horizontal edges.
    # Give them less area than the middle sections to balance the composition.
    slant = height * 0.24
    divisions = (0.21, 0.5, 0.79)
    top = [0, *(width * fraction + slant / 2 for fraction in divisions), width]
    bottom = [0, *(width * fraction - slant / 2 for fraction in divisions), width]
    normal_x = height / math.hypot(height, slant)
    gap = 18
    half_gap = gap / 2 / normal_x
    panes = []
    silhouettes = []
    for index, scene in enumerate(scenes):
        left_top = top[index] + half_gap if index else 0
        left_bottom = bottom[index] + half_gap if index else 0
        right_top = top[index + 1] - half_gap if index < len(scenes) - 1 else width
        right_bottom = bottom[index + 1] - half_gap if index < len(scenes) - 1 else width
        points = ((left_top, 0), (right_top, 0), (right_bottom, height), (left_bottom, height))
        panes.append((scene, points))
        # Blur the actual panes, including gaps and rounded outside corners.
        svg_points = " ".join(f"{px},{py}" for px, py in points)
        silhouettes.append(f'''<defs><clipPath id="pane-{index}">
          <rect width="{width}" height="{height}" rx="{radius}"/>
        </clipPath></defs>
        <polygon clip-path="url(#pane-{index})" points="{svg_points}"/>''')
    shadow(x, y, width, height, radius, 20, 16, 0.24, silhouette="".join(silhouettes))

    for index, (scene, points) in enumerate(panes):
        context.save()
        rounded(x, y, width, height, radius)
        context.clip()
        polygon(points, x, y)
        context.clip()
        image = images[scene]
        # Keep every capture at the same scale and position within the frame.
        context.save()
        context.translate(x, y)
        scale = width / image.get_width()
        context.scale(scale, scale)
        context.set_source_surface(image)
        context.get_source().set_filter(cairo.FILTER_BEST)
        context.paint()
        context.restore()

        if index < len(panes) - 1:
            reflection = cairo.LinearGradient(0, y, 0, y + height)
            for position, opacity in ((0, 0.025), (0.35, 0.18), (0.65, 0.12), (1, 0.025)):
                reflection.add_color_stop_rgba(position, *foreground, opacity)
            edge_width = 1.2 / normal_x
            right_top, right_bottom = points[1][0], points[2][0]
            polygon(
                (
                    (right_top - edge_width, 0),
                    (right_top, 0),
                    (right_bottom, height),
                    (right_bottom - edge_width, height),
                ),
                x,
                y,
            )
            context.set_source(reflection)
            context.fill()
        context.restore()
    surface.write_to_png(str(output / "banner.png"))
