from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from local_demo.renderer import Renderer
from local_demo.session import Bounds, MouseClick, MouseMove, Session, VideoSpec, Zoom


def _session(
    tmp_path: Path,
    *,
    moves=(),
    clicks=(),
    zooms=(),
    cursor_scale=1.5,
) -> Session:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"placeholder")
    return Session(
        source_path=tmp_path / "session.json",
        video=VideoSpec(source, 200, 100),
        bounds=Bounds(100, 50, 100, 50),
        process_time_start_ms=1000,
        mouse_moves=tuple(moves),
        mouse_clicks=tuple(clicks),
        zooms=tuple(zooms),
        cursor_scale=cursor_scale,
    )


def _nonwhite_bbox(image: Image.Image):
    background = Image.new("RGB", image.size, "white")
    return Image.eval(Image.fromarray(__import__("numpy").max(
        __import__("numpy").abs(
            __import__("numpy").asarray(image, dtype="int16")
            - __import__("numpy").asarray(background, dtype="int16")
        ),
        axis=2,
    ).astype("uint8")), lambda value: 255 if value else 0).getbbox()


def test_cursor_hotspot_scales_with_rendered_cursor_size(tmp_path: Path):
    # Logical (150,75) maps to source/output (100,50).  This project's original
    # arrow defines its geometric tip/hotspot at base (2,1), which becomes (4,2)
    # at cursorScale=2.  The 56x80 image therefore starts at (96,48).
    session = _session(
        tmp_path,
        moves=(MouseMove(1000, 150, 75, "arrow"),),
        cursor_scale=2.0,
    )
    rendered = Renderer(session, (200, 100)).render(0.0, Image.new("RGB", (200, 100), "white"))
    bbox = _nonwhite_bbox(rendered)
    assert bbox is not None
    left, top, right, bottom = bbox
    assert left == 96
    assert top == 48
    assert rendered.getpixel((100, 50)) != (255, 255, 255)
    assert right > 115
    assert bottom > 75


def test_click_and_cursor_share_the_zoomed_coordinate_transform(tmp_path: Path):
    # Logical (175,62.5) maps to source (150,25).  A 2x top-right camera crops
    # source [100,0,200,50], making the common output coordinate (100,50).
    moves = (MouseMove(2250, 175, 62.5, "arrow"),)
    clicks = (MouseClick(2250, 175, 62.5, "arrow", "mouseDown", "left"),)
    zooms = (Zoom(1000, 3000, 0.75, 0.25, 2.0),)
    session = _session(tmp_path, moves=moves, clicks=clicks, zooms=zooms)
    rendered = Renderer(session, (200, 100)).render(1.25, Image.new("RGB", (200, 100), "white"))

    expected_center = (100, 50)
    ring_pixel = rendered.getpixel((expected_center[0] + 12, expected_center[1]))
    assert ring_pixel[2] > ring_pixel[1] > ring_pixel[0]
    # The untransformed source coordinate must not receive a blue click ring.
    old_pixel = rendered.getpixel((150, 25))
    assert not (old_pixel[2] > old_pixel[1] + 20 and old_pixel[2] > old_pixel[0] + 20)


def test_renderer_rejects_wrong_source_frame_size(tmp_path: Path):
    renderer = Renderer(_session(tmp_path), (200, 100))
    with pytest.raises(ValueError, match="expected 200x100"):
        renderer.render(0.0, Image.new("RGB", (201, 100)))
