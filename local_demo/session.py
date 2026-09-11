"""Load and validate the first-phase rendering session format.

The loader deliberately accepts only local regular files.  Relative paths are
resolved from the session JSON rather than the process working directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple
from urllib.parse import urlparse


class SessionValidationError(ValueError):
    """Raised when a session or referenced event file is invalid."""


@dataclass(frozen=True)
class Bounds:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class VideoSpec:
    path: Path
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class MouseMove:
    process_time_ms: float
    x: float
    y: float
    cursor_id: str
    event_type: str = "mouseMoved"
    unix_time_ms: float | None = None
    active_modifiers: Tuple[Any, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MouseClick:
    process_time_ms: float
    x: float
    y: float
    cursor_id: str
    event_type: str
    button: str
    unix_time_ms: float | None = None
    active_modifiers: Tuple[Any, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Zoom:
    start_ms: float
    end_ms: float
    target_x: float
    target_y: float
    scale: float = 2.0


@dataclass(frozen=True)
class Session:
    source_path: Path
    video: VideoSpec
    bounds: Bounds
    process_time_start_ms: float
    mouse_moves: Tuple[MouseMove, ...]
    mouse_clicks: Tuple[MouseClick, ...]
    zooms: Tuple[Zoom, ...]
    output_fps: float = 30.0
    cursor_scale: float = 1.5
    version: int = 1

    def validate_frame_size(self, frame_size: tuple[int, int]) -> None:
        """Check a decoded frame against optional dimensions in the session."""
        width, height = frame_size
        if width <= 0 or height <= 0:
            raise SessionValidationError(
                f"decoded video size must be positive, got {width}x{height}"
            )
        if self.video.width is not None and width != self.video.width:
            raise SessionValidationError(
                f"video.width declares {self.video.width}, decoded video is {width}"
            )
        if self.video.height is not None and height != self.video.height:
            raise SessionValidationError(
                f"video.height declares {self.video.height}, decoded video is {height}"
            )


def _error(path: str, message: str) -> SessionValidationError:
    return SessionValidationError(f"{path}: {message}")


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    return value


def _array(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise _error(path, "must be an array")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, "must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise _error(path, "must be finite")
    if minimum is not None:
        if minimum_exclusive and result <= minimum:
            raise _error(path, f"must be greater than {minimum:g}")
        if not minimum_exclusive and result < minimum:
            raise _error(path, f"must be at least {minimum:g}")
    if maximum is not None and result > maximum:
        raise _error(path, f"must be at most {maximum:g}")
    return result


def _optional_number(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return _number(value, path)


def _positive_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "must be an integer")
    if value <= 0:
        raise _error(path, "must be positive")
    return value


def _local_file(raw_path: Any, base_dir: Path, path: str, *, suffix: str | None) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _error(path, "must be a non-empty local path")
    if "\x00" in raw_path:
        raise _error(path, "contains a null byte")
    parsed = urlparse(raw_path)
    if parsed.scheme or parsed.netloc:
        raise _error(path, "URLs and URI schemes are not allowed")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise _error(path, f"cannot resolve local file: {exc}") from exc
    if not candidate.is_file():
        raise _error(path, "must reference an existing regular file")
    if suffix is not None and candidate.suffix.lower() != suffix:
        raise _error(path, f"must reference a {suffix} file")
    return candidate


def _load_json_file(path: Path, field_path: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except UnicodeDecodeError as exc:
        raise _error(field_path, f"is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _error(
            field_path,
            f"contains invalid JSON at line {exc.lineno}, column {exc.colno}",
        ) from exc
    except OSError as exc:
        raise _error(field_path, f"could not be read: {exc}") from exc


def _event_array(value: Any, base_dir: Path, path: str) -> Sequence[Any]:
    if isinstance(value, str):
        event_path = _local_file(value, base_dir, path, suffix=".json")
        value = _load_json_file(event_path, path)
    return _array(value, path)


def _common_event(
    raw: Any,
    path: str,
    bounds: Bounds,
    process_start_ms: float,
) -> tuple[Mapping[str, Any], float, float, float, str, float | None, Tuple[Any, ...]]:
    obj = _object(raw, path)
    process_time_ms = _number(obj.get("processTimeMs"), f"{path}.processTimeMs")
    if process_time_ms < process_start_ms:
        raise _error(
            f"{path}.processTimeMs",
            "must not be earlier than processTimeStartMs",
        )
    x = _number(obj.get("x"), f"{path}.x", minimum=bounds.x, maximum=bounds.right)
    y = _number(obj.get("y"), f"{path}.y", minimum=bounds.y, maximum=bounds.bottom)
    cursor_id = obj.get("cursorId", "arrow")
    if not isinstance(cursor_id, str) or not cursor_id.strip():
        raise _error(f"{path}.cursorId", "must be a non-empty string")
    unix_time_ms = _optional_number(obj.get("unixTimeMs"), f"{path}.unixTimeMs")
    modifiers = obj.get("activeModifiers", [])
    if not isinstance(modifiers, list):
        raise _error(f"{path}.activeModifiers", "must be an array")
    return obj, process_time_ms, x, y, cursor_id, unix_time_ms, tuple(modifiers)


def _parse_moves(
    values: Sequence[Any], bounds: Bounds, process_start_ms: float
) -> Tuple[MouseMove, ...]:
    result: list[MouseMove] = []
    previous_time = -math.inf
    known = {
        "activeModifiers",
        "cursorId",
        "processTimeMs",
        "type",
        "unixTimeMs",
        "x",
        "y",
    }
    for index, raw in enumerate(values):
        path = f"mouseMoves[{index}]"
        obj, event_time, x, y, cursor_id, unix_time, modifiers = _common_event(
            raw, path, bounds, process_start_ms
        )
        event_type = obj.get("type")
        if event_type != "mouseMoved":
            raise _error(f"{path}.type", 'must be "mouseMoved"')
        if event_time < previous_time:
            raise _error(f"{path}.processTimeMs", "events must be in nondecreasing order")
        previous_time = event_time
        result.append(
            MouseMove(
                event_time,
                x,
                y,
                cursor_id,
                event_type,
                unix_time,
                modifiers,
                {key: value for key, value in obj.items() if key not in known},
            )
        )
    return tuple(result)


def _parse_clicks(
    values: Sequence[Any], bounds: Bounds, process_start_ms: float
) -> Tuple[MouseClick, ...]:
    result: list[MouseClick] = []
    previous_time = -math.inf
    known = {
        "activeModifiers",
        "button",
        "cursorId",
        "processTimeMs",
        "type",
        "unixTimeMs",
        "x",
        "y",
    }
    for index, raw in enumerate(values):
        path = f"mouseClicks[{index}]"
        obj, event_time, x, y, cursor_id, unix_time, modifiers = _common_event(
            raw, path, bounds, process_start_ms
        )
        event_type = obj.get("type")
        if event_type not in {"mouseDown", "mouseUp"}:
            raise _error(f"{path}.type", 'must be "mouseDown" or "mouseUp"')
        button = obj.get("button")
        if button != "left":
            raise _error(f"{path}.button", 'first phase supports only "left"')
        if event_time < previous_time:
            raise _error(f"{path}.processTimeMs", "events must be in nondecreasing order")
        previous_time = event_time
        result.append(
            MouseClick(
                event_time,
                x,
                y,
                cursor_id,
                event_type,
                button,
                unix_time,
                modifiers,
                {key: value for key, value in obj.items() if key not in known},
            )
        )
    return tuple(result)


def _parse_zooms(values: Sequence[Any]) -> Tuple[Zoom, ...]:
    result: list[Zoom] = []
    previous_end = -math.inf
    for index, raw in enumerate(values):
        path = f"zooms[{index}]"
        obj = _object(raw, path)
        unknown = set(obj) - {"startMs", "endMs", "target", "scale"}
        if unknown:
            name = sorted(unknown)[0]
            raise _error(f"{path}.{name}", "unknown field")
        start = _number(obj.get("startMs"), f"{path}.startMs", minimum=0)
        end = _number(obj.get("endMs"), f"{path}.endMs", minimum=0)
        if end <= start:
            raise _error(f"{path}.endMs", "must be greater than startMs")
        if start < previous_end:
            raise _error(path, "overlaps the previous zoom interval")
        target = _object(obj.get("target"), f"{path}.target")
        target_unknown = set(target) - {"x", "y"}
        if target_unknown:
            name = sorted(target_unknown)[0]
            raise _error(f"{path}.target.{name}", "unknown field")
        target_x = _number(target.get("x"), f"{path}.target.x", minimum=0, maximum=1)
        target_y = _number(target.get("y"), f"{path}.target.y", minimum=0, maximum=1)
        scale = _number(
            obj.get("scale", 2.0),
            f"{path}.scale",
            minimum=1,
            minimum_exclusive=True,
        )
        result.append(Zoom(start, end, target_x, target_y, scale))
        previous_end = end
    return tuple(result)


def load_session(path: str | Path) -> Session:
    """Load a version-1 session and all local event references."""
    source_path = Path(path).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as exc:
        raise SessionValidationError(f"session: cannot resolve local file: {exc}") from exc
    if not source_path.is_file():
        raise SessionValidationError("session: must reference an existing regular file")
    root = _object(_load_json_file(source_path, "session"), "session")
    allowed = {
        "version",
        "video",
        "bounds",
        "processTimeStartMs",
        "mouseMoves",
        "mouseClicks",
        "zooms",
        "outputFps",
        "cursorScale",
    }
    unknown = set(root) - allowed
    if unknown:
        name = sorted(unknown)[0]
        raise _error(name, "unknown top-level field")

    version = root.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise _error("version", "must be the integer 1")

    video_obj = _object(root.get("video"), "video")
    video_unknown = set(video_obj) - {"path", "width", "height"}
    if video_unknown:
        name = sorted(video_unknown)[0]
        raise _error(f"video.{name}", "unknown field")
    video_path = _local_file(
        video_obj.get("path"), source_path.parent, "video.path", suffix=".mp4"
    )
    width = (
        _positive_integer(video_obj["width"], "video.width")
        if "width" in video_obj
        else None
    )
    height = (
        _positive_integer(video_obj["height"], "video.height")
        if "height" in video_obj
        else None
    )
    if (width is None) != (height is None):
        raise _error("video", "width and height must be provided together")

    bounds_obj = _object(root.get("bounds"), "bounds")
    bounds_unknown = set(bounds_obj) - {"x", "y", "width", "height"}
    if bounds_unknown:
        name = sorted(bounds_unknown)[0]
        raise _error(f"bounds.{name}", "unknown field")
    bounds = Bounds(
        _number(bounds_obj.get("x"), "bounds.x"),
        _number(bounds_obj.get("y"), "bounds.y"),
        _number(bounds_obj.get("width"), "bounds.width", minimum=0, minimum_exclusive=True),
        _number(bounds_obj.get("height"), "bounds.height", minimum=0, minimum_exclusive=True),
    )
    process_start = _number(
        root.get("processTimeStartMs"), "processTimeStartMs", minimum=0
    )
    if "mouseMoves" not in root:
        raise _error("mouseMoves", "is required")
    if "mouseClicks" not in root:
        raise _error("mouseClicks", "is required")
    moves = _parse_moves(
        _event_array(root["mouseMoves"], source_path.parent, "mouseMoves"),
        bounds,
        process_start,
    )
    clicks = _parse_clicks(
        _event_array(root["mouseClicks"], source_path.parent, "mouseClicks"),
        bounds,
        process_start,
    )
    zooms = _parse_zooms(_array(root.get("zooms", []), "zooms"))
    output_fps = _number(root.get("outputFps", 30), "outputFps", minimum=0, minimum_exclusive=True)
    if output_fps > 240:
        raise _error("outputFps", "must be at most 240")
    cursor_scale = _number(
        root.get("cursorScale", 1.5),
        "cursorScale",
        minimum=0,
        minimum_exclusive=True,
    )
    if cursor_scale > 8:
        raise _error("cursorScale", "must be at most 8")

    return Session(
        source_path=source_path,
        video=VideoSpec(video_path, width, height),
        bounds=bounds,
        process_time_start_ms=process_start,
        mouse_moves=moves,
        mouse_clicks=clicks,
        zooms=zooms,
        output_fps=output_fps,
        cursor_scale=cursor_scale,
        version=version,
    )

