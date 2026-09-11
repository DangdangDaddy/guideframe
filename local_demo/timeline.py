"""Deterministic cursor and camera sampling on the output time grid."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import Sequence, Tuple

from .session import Bounds, MouseClick, MouseMove, Zoom


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class CursorState:
    position: Point
    cursor_id: str


@dataclass(frozen=True)
class CameraState:
    frame_width: int
    frame_height: int
    scale: float
    center_x: float
    center_y: float

    @property
    def crop_box(self) -> tuple[float, float, float, float]:
        """Floating source-pixel EXTENT box: left, top, right, bottom."""
        crop_width = self.frame_width / self.scale
        crop_height = self.frame_height / self.scale
        return (
            self.center_x - crop_width / 2,
            self.center_y - crop_height / 2,
            self.center_x + crop_width / 2,
            self.center_y + crop_height / 2,
        )

    def map_point(self, point: Point) -> Point:
        left, top, _, _ = self.crop_box
        return Point((point.x - left) * self.scale, (point.y - top) * self.scale)


def map_recording_point(
    x: float,
    y: float,
    bounds: Bounds,
    frame_size: tuple[int, int],
) -> Point:
    """Map top-left-origin display logical coordinates to source pixels."""
    width, height = frame_size
    if width <= 0 or height <= 0:
        raise ValueError("frame_size values must be positive")
    if not all(math.isfinite(value) for value in (x, y)):
        raise ValueError("point coordinates must be finite")
    return Point(
        (x - bounds.x) * width / bounds.width,
        (y - bounds.y) * height / bounds.height,
    )


def _relative_time(process_time_ms: float, process_start_ms: float) -> float:
    return (process_time_ms - process_start_ms) / 1000.0


def _denoise_moves(
    moves: Sequence[MouseMove], process_start_ms: float, window_seconds: float = 0.055
) -> list[tuple[float, Point]]:
    """Symmetric Gaussian smoothing that keeps endpoints and cannot overshoot.

    A straight constant-speed segment is left effectively unchanged, while
    short alternating jitter is averaged.  Clicks are inserted afterwards as
    exact anchors, so this filter can never shift a click hit.
    """
    raw: list[tuple[float, Point]] = []
    for move in moves:
        item = (
            _relative_time(move.process_time_ms, process_start_ms),
            Point(move.x, move.y),
        )
        # Position follows the final record at a duplicate timestamp.  Shape
        # keeps its own uncollapsed sequence below.
        if raw and item[0] == raw[-1][0]:
            raw[-1] = item
        else:
            raw.append(item)
    if len(raw) < 3:
        return raw
    result: list[tuple[float, Point]] = []
    for index, (time, point) in enumerate(raw):
        if index == 0 or index == len(raw) - 1:
            result.append((time, point))
            continue
        neighbors: list[tuple[float, Point]] = []
        left = index
        while left >= 0 and time - raw[left][0] <= window_seconds:
            neighbors.append(raw[left])
            left -= 1
        right = index + 1
        while right < len(raw) and raw[right][0] - time <= window_seconds:
            neighbors.append(raw[right])
            right += 1
        if len(neighbors) < 3:
            result.append((time, point))
            continue
        weighted_x = 0.0
        weighted_y = 0.0
        weight_sum = 0.0
        for neighbor_time, neighbor in neighbors:
            ratio = (neighbor_time - time) / window_seconds
            weight = math.exp(-2.0 * ratio * ratio)
            weighted_x += neighbor.x * weight
            weighted_y += neighbor.y * weight
            weight_sum += weight
        result.append((time, Point(weighted_x / weight_sum, weighted_y / weight_sum)))
    return result


def _collapse_position_knots(
    moves: Sequence[MouseMove],
    clicks: Sequence[MouseClick],
    process_start_ms: float,
) -> list[tuple[float, Point]]:
    # priority 0: denoised move, priority 1: mouseDown.  Python's stable sort
    # and replacement below mean a later record wins within one priority, and
    # a click always wins a move at exactly the same timestamp.
    records: list[tuple[float, int, int, Point]] = []
    for index, (time, point) in enumerate(_denoise_moves(moves, process_start_ms)):
        records.append((time, 0, index, point))
    for index, click in enumerate(clicks):
        if click.event_type == "mouseDown":
            records.append(
                (
                    _relative_time(click.process_time_ms, process_start_ms),
                    1,
                    index,
                    Point(click.x, click.y),
                )
            )
    records.sort(key=lambda item: (item[0], item[1], item[2]))
    collapsed: list[tuple[float, Point]] = []
    for time, _, _, point in records:
        if collapsed and time == collapsed[-1][0]:
            collapsed[-1] = (time, point)
        else:
            collapsed.append((time, point))
    return collapsed


def _pchip_derivatives(times: Sequence[float], values: Sequence[float]) -> list[float]:
    count = len(times)
    if count == 1:
        return [0.0]
    h = [times[i + 1] - times[i] for i in range(count - 1)]
    slopes = [(values[i + 1] - values[i]) / h[i] for i in range(count - 1)]
    if count == 2:
        return [slopes[0], slopes[0]]

    derivatives = [0.0] * count
    for i in range(1, count - 1):
        before = slopes[i - 1]
        after = slopes[i]
        if before == 0.0 or after == 0.0 or before * after < 0.0:
            derivatives[i] = 0.0
        else:
            before_weight = 2.0 * h[i] + h[i - 1]
            after_weight = h[i] + 2.0 * h[i - 1]
            derivatives[i] = (before_weight + after_weight) / (
                before_weight / before + after_weight / after
            )

    first = ((2.0 * h[0] + h[1]) * slopes[0] - h[0] * slopes[1]) / (h[0] + h[1])
    if first * slopes[0] <= 0.0:
        first = 0.0
    elif slopes[0] * slopes[1] < 0.0 and abs(first) > abs(3.0 * slopes[0]):
        first = 3.0 * slopes[0]
    last = ((2.0 * h[-1] + h[-2]) * slopes[-1] - h[-1] * slopes[-2]) / (
        h[-1] + h[-2]
    )
    if last * slopes[-1] <= 0.0:
        last = 0.0
    elif slopes[-1] * slopes[-2] < 0.0 and abs(last) > abs(3.0 * slopes[-1]):
        last = 3.0 * slopes[-1]
    derivatives[0] = first
    derivatives[-1] = last
    return derivatives


def _hermite(
    start_value: float,
    end_value: float,
    start_derivative: float,
    end_derivative: float,
    duration: float,
    amount: float,
) -> float:
    amount2 = amount * amount
    amount3 = amount2 * amount
    return (
        (2 * amount3 - 3 * amount2 + 1) * start_value
        + (amount3 - 2 * amount2 + amount) * duration * start_derivative
        + (-2 * amount3 + 3 * amount2) * end_value
        + (amount3 - amount2) * duration * end_derivative
    )


class CursorTimeline:
    """Denoised, no-overshoot cursor position plus independent shape state."""

    def __init__(
        self,
        moves: Sequence[MouseMove],
        clicks: Sequence[MouseClick],
        process_time_start_ms: float,
    ) -> None:
        knots = _collapse_position_knots(moves, clicks, process_time_start_ms)
        self._times = tuple(time for time, _ in knots)
        self._xs = tuple(point.x for _, point in knots)
        self._ys = tuple(point.y for _, point in knots)
        self._dx = tuple(_pchip_derivatives(self._times, self._xs)) if knots else ()
        self._dy = tuple(_pchip_derivatives(self._times, self._ys)) if knots else ()

        # Shape is intentionally independent from smoothed position.  Keeping
        # duplicate times here makes bisect_right select the final input record.
        self._shape_times = tuple(
            _relative_time(move.process_time_ms, process_time_start_ms) for move in moves
        )
        self._shape_ids = tuple(move.cursor_id for move in moves)

    def sample(self, time_seconds: float) -> CursorState | None:
        if not math.isfinite(time_seconds):
            raise ValueError("time_seconds must be finite")
        if not self._times or time_seconds < self._times[0]:
            return None
        if time_seconds >= self._times[-1]:
            point = Point(self._xs[-1], self._ys[-1])
        else:
            index = bisect_right(self._times, time_seconds) - 1
            start_time = self._times[index]
            end_time = self._times[index + 1]
            amount = (time_seconds - start_time) / (end_time - start_time)
            point = Point(
                _hermite(
                    self._xs[index],
                    self._xs[index + 1],
                    self._dx[index],
                    self._dx[index + 1],
                    end_time - start_time,
                    amount,
                ),
                _hermite(
                    self._ys[index],
                    self._ys[index + 1],
                    self._dy[index],
                    self._dy[index + 1],
                    end_time - start_time,
                    amount,
                ),
            )
        shape_index = bisect_right(self._shape_times, time_seconds) - 1
        cursor_id = self._shape_ids[shape_index] if shape_index >= 0 else "arrow"
        return CursorState(point, cursor_id)


def _smoothstep(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


class ZoomTimeline:
    ENTER_EXIT_SECONDS = 0.250

    def __init__(self, zooms: Sequence[Zoom]) -> None:
        self._zooms = tuple(zooms)
        self._start_times = tuple(zoom.start_ms / 1000.0 for zoom in zooms)

    def sample(self, time_seconds: float, frame_size: tuple[int, int]) -> CameraState:
        if not math.isfinite(time_seconds):
            raise ValueError("time_seconds must be finite")
        width, height = frame_size
        if width <= 0 or height <= 0:
            raise ValueError("frame_size values must be positive")
        base_x = width / 2.0
        base_y = height / 2.0
        index = bisect_right(self._start_times, time_seconds) - 1
        if index < 0:
            return CameraState(width, height, 1.0, base_x, base_y)
        zoom = self._zooms[index]
        start = zoom.start_ms / 1000.0
        end = zoom.end_ms / 1000.0
        if time_seconds < start or time_seconds >= end:
            return CameraState(width, height, 1.0, base_x, base_y)

        duration = end - start
        transition = min(self.ENTER_EXIT_SECONDS, duration / 2.0)
        if time_seconds < start + transition:
            activation = _smoothstep((time_seconds - start) / transition)
        elif time_seconds >= end - transition:
            activation = _smoothstep((end - time_seconds) / transition)
        else:
            activation = 1.0

        scale = 1.0 + (zoom.scale - 1.0) * activation
        desired_x = base_x + (zoom.target_x * width - base_x) * activation
        desired_y = base_y + (zoom.target_y * height - base_y) * activation
        half_width = width / (2.0 * scale)
        half_height = height / (2.0 * scale)
        center_x = min(width - half_width, max(half_width, desired_x))
        center_y = min(height - half_height, max(half_height, desired_y))
        return CameraState(width, height, scale, center_x, center_y)
