from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from local_demo.session import (
    Bounds,
    MouseClick,
    MouseMove,
    SessionValidationError,
    Zoom,
    load_session,
)
from local_demo.timeline import CursorTimeline, Point, ZoomTimeline, map_recording_point


def _event(event_type: str, time_ms: float, x: float, y: float, **extra):
    event = {
        "activeModifiers": [],
        "cursorId": "arrow",
        "processTimeMs": time_ms,
        "type": event_type,
        "unixTimeMs": 1_800_000_000_000.0 + time_ms,
        "x": x,
        "y": y,
    }
    event.update(extra)
    return event


def _valid_document(video_name: str = "source.mp4"):
    return {
        "version": 1,
        "video": {"path": video_name, "width": 1280, "height": 720},
        "bounds": {"x": 100, "y": 50, "width": 640, "height": 360},
        "processTimeStartMs": 1000,
        "mouseMoves": [
            _event("mouseMoved", 1100, 100, 50),
            _event("mouseMoved", 2000, 740, 410),
        ],
        "mouseClicks": [
            _event("mouseDown", 1500, 420, 230, button="left"),
            _event("mouseUp", 1570, 420, 230, button="left"),
        ],
        "zooms": [
            {"startMs": 1000, "endMs": 3000, "target": {"x": 0.95, "y": 0.1}, "scale": 2}
        ],
        "outputFps": 30,
        "cursorScale": 1.5,
    }


def _write_session(tmp_path: Path, document) -> Path:
    (tmp_path / "source.mp4").write_bytes(b"local placeholder")
    path = tmp_path / "session.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_nonzero_bounds_and_retina_mapping_use_independent_formula():
    bounds = Bounds(x=100, y=50, width=640, height=360)
    mapped = map_recording_point(420, 230, bounds, (1280, 720))
    # Independent expectation: subtract the recording origin, then multiply by
    # the actual video/bounds ratio (2 on each axis in this fixture).
    assert mapped == Point(640, 360)
    assert map_recording_point(100, 50, bounds, (1280, 720)) == Point(0, 0)
    assert map_recording_point(740, 410, bounds, (1280, 720)) == Point(1280, 720)


def test_mapping_supports_negative_display_origin_and_nonuniform_ratios():
    bounds = Bounds(x=-300, y=80, width=400, height=200)
    mapped = map_recording_point(-100, 180, bounds, (1000, 400))
    assert mapped.x == pytest.approx(500)
    assert mapped.y == pytest.approx(200)


def test_equal_time_moves_are_kept_and_final_shape_wins():
    moves = (
        MouseMove(1100, 10, 20, "arrow"),
        MouseMove(1100, 10, 20, "pointingHand"),
        MouseMove(2000, 90, 80, "arrow"),
    )
    timeline = CursorTimeline(moves, (), 1000)
    assert timeline.sample(0.099) is None
    state = timeline.sample(0.1)
    assert state is not None
    assert state.position == Point(10, 20)
    assert state.cursor_id == "pointingHand"
    assert timeline.sample(3.0).position == Point(90, 80)


def test_mouse_down_is_an_exact_position_anchor_despite_sparse_moves():
    moves = (
        MouseMove(1100, 10, 10, "arrow"),
        MouseMove(2000, 90, 90, "arrow"),
    )
    clicks = (MouseClick(1500, 60, 35, "arrow", "mouseDown", "left"),)
    timeline = CursorTimeline(moves, clicks, 1000)
    at_click = timeline.sample(0.5)
    assert at_click is not None
    assert at_click.position.x == pytest.approx(60, abs=1e-12)
    assert at_click.position.y == pytest.approx(35, abs=1e-12)


def test_sparse_smoothing_does_not_overshoot_segment_bounds():
    moves = (
        MouseMove(1000, 0, 0, "arrow"),
        MouseMove(2000, 100, 20, "arrow"),
        MouseMove(5000, 120, 80, "arrow"),
    )
    timeline = CursorTimeline(moves, (), 1000)
    for tick in range(101):
        state = timeline.sample(tick * 0.04)
        assert state is not None
        assert -1e-9 <= state.position.x <= 120 + 1e-9
        assert -1e-9 <= state.position.y <= 80 + 1e-9


def test_dense_jitter_is_measurably_smoothed():
    raw_x = [50, 54, 46, 55, 45, 50]
    moves = tuple(
        MouseMove(1000 + index * 10, x, 30, "arrow")
        for index, x in enumerate(raw_x)
    )
    timeline = CursorTimeline(moves, (), 1000)
    smoothed_x = [timeline.sample(index * 0.01).position.x for index in range(len(raw_x))]

    raw_total_variation = sum(abs(b - a) for a, b in zip(raw_x, raw_x[1:]))
    smooth_total_variation = sum(
        abs(b - a) for a, b in zip(smoothed_x, smoothed_x[1:])
    )
    raw_second_difference = sum(
        abs(raw_x[index + 1] - 2 * raw_x[index] + raw_x[index - 1])
        for index in range(1, len(raw_x) - 1)
    )
    smooth_second_difference = sum(
        abs(smoothed_x[index + 1] - 2 * smoothed_x[index] + smoothed_x[index - 1])
        for index in range(1, len(smoothed_x) - 1)
    )
    assert smoothed_x[1] != pytest.approx(raw_x[1])
    assert smooth_total_variation < raw_total_variation * 0.5
    assert smooth_second_difference < raw_second_difference * 0.5


@pytest.mark.parametrize(
    ("time_seconds", "expected_scale"),
    [
        (0.999, 1.0),
        (1.000, 1.0),
        (1.125, 1.5),
        (1.250, 2.0),
        (2.750, 2.0),
        (2.875, 1.5),
        (3.000, 1.0),
        (3.001, 1.0),
    ],
)
def test_zoom_enter_hold_exit_boundaries(time_seconds: float, expected_scale: float):
    camera = ZoomTimeline((Zoom(1000, 3000, 0.95, 0.1, 2.0),)).sample(
        time_seconds, (1280, 720)
    )
    assert camera.scale == pytest.approx(expected_scale, abs=1e-12)


def test_zoom_edge_clamp_has_no_source_outside_crop_and_maps_consistently():
    camera = ZoomTimeline((Zoom(1000, 3000, 0.95, 0.1, 2.0),)).sample(
        2.0, (1280, 720)
    )
    assert camera.center_x == pytest.approx(960)
    assert camera.center_y == pytest.approx(180)
    assert camera.crop_box == pytest.approx((640, 0, 1280, 360))
    output = camera.map_point(Point(1216, 72))
    assert output == Point(1152, 144)
    left, top, right, bottom = camera.crop_box
    assert 0 <= left < right <= 1280
    assert 0 <= top < bottom <= 720


@pytest.mark.parametrize(
    ("time_seconds", "expected_scale"),
    [(1.0, 1.0), (1.075, 1.5), (1.15, 2.0), (1.225, 1.5), (1.3, 1.0)],
)
def test_short_zoom_splits_interval_evenly_without_a_hold(time_seconds, expected_scale):
    camera = ZoomTimeline((Zoom(1000, 1300, 0.5, 0.5, 2.0),)).sample(
        time_seconds, (800, 600)
    )
    assert camera.scale == pytest.approx(expected_scale, abs=1e-12)


def test_loader_resolves_event_files_relative_to_session(tmp_path: Path):
    document = _valid_document()
    moves = document.pop("mouseMoves")
    clicks = document.pop("mouseClicks")
    (tmp_path / "moves.json").write_text(json.dumps(moves), encoding="utf-8")
    (tmp_path / "clicks.json").write_text(json.dumps(clicks), encoding="utf-8")
    document["mouseMoves"] = "moves.json"
    document["mouseClicks"] = "clicks.json"
    session = load_session(_write_session(tmp_path, document))
    assert len(session.mouse_moves) == 2
    assert len(session.mouse_clicks) == 2
    assert session.video.path == (tmp_path / "source.mp4").resolve()


def test_loader_allows_equal_timestamps_but_rejects_decreasing_events(tmp_path: Path):
    equal = _valid_document()
    equal["mouseMoves"][1]["processTimeMs"] = 1100
    session = load_session(_write_session(tmp_path, equal))
    assert len(session.mouse_moves) == 2

    decreasing = _valid_document()
    decreasing["mouseMoves"][1]["processTimeMs"] = 1050
    (tmp_path / "session.json").write_text(json.dumps(decreasing), encoding="utf-8")
    with pytest.raises(SessionValidationError, match=r"mouseMoves\[1\]\.processTimeMs"):
        load_session(tmp_path / "session.json")


def test_loader_rejects_overlap_and_reports_second_zoom_index(tmp_path: Path):
    document = _valid_document()
    document["zooms"].append(
        {"startMs": 2999, "endMs": 4000, "target": {"x": 0.5, "y": 0.5}, "scale": 1.5}
    )
    with pytest.raises(SessionValidationError, match=r"zooms\[1\].*overlaps"):
        load_session(_write_session(tmp_path, document))


def test_loader_allows_touching_zoom_intervals(tmp_path: Path):
    document = _valid_document()
    document["zooms"].append(
        {"startMs": 3000, "endMs": 4000, "target": {"x": 0.5, "y": 0.5}, "scale": 1.5}
    )
    assert len(load_session(_write_session(tmp_path, document)).zooms) == 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc["video"].update(path="https://example.invalid/video.mp4"), "URLs"),
        (lambda doc: doc["mouseMoves"][0].update(processTimeMs=999), "processTimeStartMs"),
        (lambda doc: doc["mouseClicks"][0].update(button="right"), 'only "left"'),
        (lambda doc: doc.update(outputFps=math.inf), "finite"),
        (lambda doc: doc.update(unexpected=True), "unknown top-level"),
    ],
)
def test_loader_rejects_unsafe_or_unsupported_input(tmp_path: Path, mutation, message):
    document = _valid_document()
    mutation(document)
    with pytest.raises(SessionValidationError, match=message):
        load_session(_write_session(tmp_path, document))
