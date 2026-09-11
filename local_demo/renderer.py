"""Pillow frame compositor shared by MP4, GIF, and PNG exports."""

from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import math

from PIL import Image, ImageDraw

from .session import Session, SessionValidationError
from .timeline import CameraState, CursorTimeline, Point, ZoomTimeline, map_recording_point


CLICK_DURATION_SECONDS = 0.380


class Renderer:
    """Render session effects at an exact source-video time in seconds."""

    def __init__(self, session: Session, frame_size: tuple[int, int]) -> None:
        session.validate_frame_size(frame_size)
        self.session = session
        self.frame_size = tuple(frame_size)
        self.cursor_timeline = CursorTimeline(
            session.mouse_moves, session.mouse_clicks, session.process_time_start_ms
        )
        self.zoom_timeline = ZoomTimeline(session.zooms)
        self._mouse_downs = tuple(
            click for click in session.mouse_clicks if click.event_type == "mouseDown"
        )
        self._mouse_down_times = tuple(
            (click.process_time_ms - session.process_time_start_ms) / 1000.0
            for click in self._mouse_downs
        )

    def render(self, time_seconds: float, source: Image.Image) -> Image.Image:
        if not math.isfinite(time_seconds) or time_seconds < 0:
            raise ValueError("time_seconds must be a finite non-negative number")
        if source.size != self.frame_size:
            raise SessionValidationError(
                f"source frame is {source.width}x{source.height}, expected "
                f"{self.frame_size[0]}x{self.frame_size[1]}"
            )
        source_rgb = source.convert("RGB") if source.mode != "RGB" else source
        camera = self.zoom_timeline.sample(time_seconds, self.frame_size)
        if abs(camera.scale - 1.0) < 1e-12:
            composed = source_rgb.copy()
        else:
            composed = source_rgb.transform(
                self.frame_size,
                Image.Transform.EXTENT,
                camera.crop_box,
                # EXTENT supports floating source coordinates, but Pillow only
                # permits nearest/bilinear/bicubic for geometric transforms.
                resample=Image.Resampling.BICUBIC,
            )
        canvas = composed.convert("RGBA")
        self._draw_clicks(canvas, time_seconds, camera)
        self._draw_cursor(canvas, time_seconds, camera)
        return canvas.convert("RGB")

    def render_frame(self, time_seconds: float, source: Image.Image) -> Image.Image:
        """Compatibility alias for media pipelines."""
        return self.render(time_seconds, source)

    def _source_point(self, x: float, y: float) -> Point:
        return map_recording_point(x, y, self.session.bounds, self.frame_size)

    def _draw_clicks(
        self, canvas: Image.Image, time_seconds: float, camera: CameraState
    ) -> None:
        first = bisect_right(
            self._mouse_down_times, time_seconds - CLICK_DURATION_SECONDS
        )
        last = bisect_right(self._mouse_down_times, time_seconds)
        if first == last:
            return
        overlay = Image.new("RGBA", self.frame_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for index in range(first, last):
            click = self._mouse_downs[index]
            event_time = self._mouse_down_times[index]
            age = time_seconds - event_time
            progress = age / CLICK_DURATION_SECONDS
            eased = progress * progress * (3.0 - 2.0 * progress)
            radius = 12.0 + (56.0 - 12.0) * eased
            alpha = round(255 * 0.75 * (1.0 - eased))
            center = camera.map_point(self._source_point(click.x, click.y))
            width = max(2, round(4.0 * (1.0 - 0.35 * progress)))
            draw.ellipse(
                (
                    center.x - radius,
                    center.y - radius,
                    center.x + radius,
                    center.y + radius,
                ),
                outline=(48, 140, 255, alpha),
                width=width,
            )
        canvas.alpha_composite(overlay)

    def _draw_cursor(
        self, canvas: Image.Image, time_seconds: float, camera: CameraState
    ) -> None:
        state = self.cursor_timeline.sample(time_seconds)
        if state is None:
            return
        source_point = self._source_point(state.position.x, state.position.y)
        output_point = camera.map_point(source_point)
        cursor, hotspot = _cursor_image(state.cursor_id, self.session.cursor_scale)
        left = round(output_point.x - hotspot.x)
        top = round(output_point.y - hotspot.y)
        canvas.alpha_composite(cursor, dest=(left, top))


@lru_cache(maxsize=32)
def _cursor_image(cursor_id: str, cursor_scale: float) -> tuple[Image.Image, Point]:
    normalized = cursor_id.lower()
    if normalized in {"pointinghand", "pointing_hand", "hand"}:
        kind = "hand"
        base_width, base_height = 32, 32
        hotspot = Point(14.5, 3.0)
    elif normalized in {"ibeam", "i_beam", "text"}:
        kind = "ibeam"
        base_width, base_height = 18, 32
        hotspot = Point(9.0, 16.0)
    else:
        kind = "arrow"
        base_width, base_height = 28, 40
        hotspot = Point(2.0, 1.0)

    width = max(4, round(base_width * cursor_scale))
    height = max(4, round(base_height * cursor_scale))
    supersample = 4
    image = Image.new("RGBA", (width * supersample, height * supersample), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def points(values: list[tuple[float, float]]) -> list[tuple[int, int]]:
        return [
            (
                round(x * width / base_width * supersample),
                round(y * height / base_height * supersample),
            )
            for x, y in values
        ]

    stroke = max(2, round(1.7 * cursor_scale * supersample))
    if kind == "arrow":
        polygon = points(
            [(2, 1), (24, 21), (14, 22), (20, 36), (14, 39), (8, 25), (2, 32)]
        )
        draw.polygon(polygon, fill=(250, 250, 250, 255))
        draw.line(polygon + [polygon[0]], fill=(20, 20, 20, 255), width=stroke, joint="curve")
    elif kind == "ibeam":
        bars = points([(3, 2), (15, 2), (15, 6), (11, 6), (11, 26), (15, 26), (15, 30), (3, 30), (3, 26), (7, 26), (7, 6), (3, 6)])
        draw.polygon(bars, fill=(250, 250, 250, 255))
        draw.line(bars + [bars[0]], fill=(20, 20, 20, 255), width=stroke, joint="curve")
    else:
        hand = points(
            [
                (13, 3), (16, 3), (17, 15), (18, 11), (21, 11),
                (22, 15), (23, 12), (26, 13), (27, 17), (28, 16), (31, 18),
                (29, 25), (25, 30), (14, 30), (10, 25), (6, 20), (7, 17),
                (10, 18), (13, 21),
            ]
        )
        draw.polygon(hand, fill=(250, 250, 250, 255))
        draw.line(hand + [hand[0]], fill=(20, 20, 20, 255), width=stroke, joint="curve")

    image = image.resize((width, height), Image.Resampling.LANCZOS)
    rendered_hotspot = Point(
        hotspot.x * width / base_width,
        hotspot.y * height / base_height,
    )
    return image, rendered_hotspot
