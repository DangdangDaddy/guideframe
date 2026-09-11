"""Generate the fully synthetic input used by the local demo and acceptance checks.

The artwork, pointer targets, event data, and audio are created locally.  Nothing
in this module reads Screen Studio projects or bundles third-party media.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from PIL import Image, ImageDraw, ImageFont


DEMO_WIDTH = 1280
DEMO_HEIGHT = 720
DEMO_DURATION = 7.0
DEMO_FPS = 30
PROCESS_TIME_START_MS = 1000.0
BOUNDS = {"x": 100.0, "y": 50.0, "width": 640.0, "height": 360.0}


@dataclass(frozen=True)
class DemoInputs:
    output_dir: Path
    source_video: Path
    session: Path
    mouse_moves: Path
    mouse_clicks: Path


@dataclass(frozen=True)
class DemoArtifacts:
    inputs: DemoInputs
    rendered_mp4: Path
    rendered_gif: Path
    rendered_png: Path


def _ffmpeg_executable() -> str:
    """Return the pinned wheel's ffmpeg, without consulting PATH."""
    try:
        from .media import ffmpeg_executable
    except ImportError as exc:  # pragma: no cover - exercised by install check
        raise RuntimeError(
            "imageio-ffmpeg is required; install the project dependencies first"
        ) from exc
    executable = ffmpeg_executable()
    if not Path(executable).is_file():
        raise RuntimeError(f"bundled ffmpeg was not found: {executable}")
    return str(executable)


def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _target(draw: ImageDraw.ImageDraw, xy: Tuple[int, int], label: str) -> None:
    x, y = xy
    draw.ellipse((x - 13, y - 13, x + 13, y + 13), fill="#ffffff", outline="#ef4444", width=4)
    draw.line((x - 18, y, x + 18, y), fill="#ef4444", width=2)
    draw.line((x, y - 18, x, y + 18), fill="#ef4444", width=2)
    draw.text((x + 20, y - 12), label, fill="#7f1d1d", font=_font(18))


def _draw_source_frame(index: int) -> Image.Image:
    """Draw an original, cursor-free UI frame with known geometric targets."""
    image = Image.new("RGB", (DEMO_WIDTH, DEMO_HEIGHT), "#e8eef7")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((48, 38, 1232, 682), radius=24, fill="#ffffff", outline="#cbd5e1", width=2)
    draw.rounded_rectangle((48, 38, 1232, 104), radius=24, fill="#172554")
    draw.rectangle((48, 78, 1232, 104), fill="#172554")
    draw.ellipse((76, 60, 92, 76), fill="#fb7185")
    draw.ellipse((101, 60, 117, 76), fill="#fbbf24")
    draw.ellipse((126, 60, 142, 76), fill="#4ade80")
    draw.text((178, 56), "LOCAL WORKFLOW · SYNTHETIC DEMO", fill="#f8fafc", font=_font(24))

    draw.rectangle((48, 104, 260, 682), fill="#f1f5f9")
    sections = ("Overview", "Capture", "Polish", "Export")
    selected = index % len(sections)
    for item_index, label in enumerate(sections):
        top = 145 + item_index * 68
        fill = "#dbeafe" if item_index == selected else "#f1f5f9"
        draw.rounded_rectangle((75, top, 230, top + 46), radius=12, fill=fill)
        draw.text((99, top + 11), label, fill="#1e3a8a" if item_index == selected else "#475569", font=_font(19))

    draw.text((300, 135), "Build a clear product walkthrough", fill="#0f172a", font=_font(31))
    draw.text((300, 180), "Every marker has a known source-pixel coordinate.", fill="#64748b", font=_font(19))

    card_colors = ("#dbeafe", "#dcfce7", "#fef3c7")
    card_titles = ("Record", "Guide", "Share")
    for card_index, left in enumerate((300, 590, 880)):
        draw.rounded_rectangle((left, 240, left + 250, 405), radius=18, fill=card_colors[card_index])
        draw.text((left + 24, 268), card_titles[card_index], fill="#0f172a", font=_font(25))
        draw.rounded_rectangle((left + 24, 326, left + 205, 347), radius=8, fill="#ffffff")
        draw.rounded_rectangle((left + 24, 361, left + 150, 378), radius=7, fill="#ffffff")

    progress = min(1.0, index / 5.0)
    draw.text((300, 493), "Synthetic source state", fill="#334155", font=_font(21))
    draw.rounded_rectangle((300, 535, 1110, 563), radius=14, fill="#e2e8f0")
    draw.rounded_rectangle((300, 535, 300 + int(810 * progress), 563), radius=14, fill="#2563eb")
    draw.text((300, 592), f"VFR source card {index + 1}/7", fill="#64748b", font=_font(18))

    # These targets make mapping, click anchoring, and camera edge clamps visible.
    _target(draw, (300, 180), "A")
    _target(draw, (1040, 180), "B")
    _target(draw, (1040, 560), "C")
    _target(draw, (160, 580), "D")
    _target(draw, (640, 360), "E")
    return image


def _write_audio(path: Path) -> None:
    sample_rate = 48_000
    click_times = (1.50, 2.65, 4.85)
    frame_count = int(DEMO_DURATION * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for sample_index in range(frame_count):
            t = sample_index / sample_rate
            value = 0.035 * math.sin(2.0 * math.pi * 220.0 * t)
            for click_time in click_times:
                dt = t - click_time
                if 0.0 <= dt < 0.045:
                    value += 0.45 * (1.0 - dt / 0.045) * math.sin(2.0 * math.pi * 1320.0 * dt)
            value = max(-1.0, min(1.0, value))
            frames.extend(struct.pack("<h", int(round(value * 32767))))
        wav.writeframes(frames)


def _run(command: Iterable[str]) -> None:
    completed = subprocess.run(
        list(command),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip().splitlines()[-12:]
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(detail))


def _write_vfr_source(path: Path) -> None:
    # Irregular PTS changes verify that source-frame holding and CFR animation are
    # separate concerns.  The final duplicate is required by concat demuxer.
    pts = (0.0, 0.70, 1.90, 3.30, 4.80, 6.20, 6.96, DEMO_DURATION)
    with tempfile.TemporaryDirectory(prefix="local-demo-") as temp_name:
        temp_dir = Path(temp_name)
        frame_paths: List[Path] = []
        for index in range(7):
            frame_path = temp_dir / f"frame-{index:02d}.png"
            _draw_source_frame(index).save(frame_path)
            frame_paths.append(frame_path)
        concat_path = temp_dir / "frames.txt"
        concat_lines: List[str] = []
        for index, frame_path in enumerate(frame_paths):
            concat_lines.append(f"file '{frame_path.as_posix()}'")
            concat_lines.append(f"duration {pts[index + 1] - pts[index]:.6f}")
        concat_lines.append(f"file '{frame_paths[-1].as_posix()}'")
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        audio_path = temp_dir / "synthetic-audio.wav"
        _write_audio(audio_path)
        _run(
            (
                _ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-i",
                str(audio_path),
                "-t",
                f"{DEMO_DURATION:.3f}",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                "1000",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-y",
                str(path),
            )
        )


def _logical_point(source_x: float, source_y: float) -> Tuple[float, float]:
    return (
        BOUNDS["x"] + source_x * BOUNDS["width"] / DEMO_WIDTH,
        BOUNDS["y"] + source_y * BOUNDS["height"] / DEMO_HEIGHT,
    )


def _event(event_type: str, seconds: float, source_xy: Tuple[float, float], cursor_id: str = "arrow", **extra: Any) -> Dict[str, Any]:
    x, y = _logical_point(*source_xy)
    process_time_ms = PROCESS_TIME_START_MS + seconds * 1000.0
    result: Dict[str, Any] = {
        "activeModifiers": [],
        "cursorId": cursor_id,
        "processTimeMs": process_time_ms,
        "type": event_type,
        "unixTimeMs": 1_800_000_000_000.0 + process_time_ms,
        "x": x,
        "y": y,
    }
    result.update(extra)
    return result


def _session_document() -> Dict[str, Any]:
    """Return the demo session document; kept in one place for schema updates."""
    return {
        "version": 1,
        "video": {
            "path": "source-vfr.mp4",
            "width": DEMO_WIDTH,
            "height": DEMO_HEIGHT,
        },
        "bounds": BOUNDS,
        "processTimeStartMs": PROCESS_TIME_START_MS,
        "mouseMoves": "mousemoves.json",
        "mouseClicks": "mouseclicks.json",
        "outputFps": DEMO_FPS,
        "cursorScale": 1.5,
        "zooms": [
            {
                "startMs": 1300.0,
                "endMs": 3300.0,
                "target": {"x": 300.0 / DEMO_WIDTH, "y": 180.0 / DEMO_HEIGHT},
                "scale": 2.0,
            },
            {
                "startMs": 4200.0,
                "endMs": 6400.0,
                "target": {"x": 1040.0 / DEMO_WIDTH, "y": 560.0 / DEMO_HEIGHT},
                "scale": 2.0,
            },
        ],
    }


def create_demo_inputs(output_dir: Path) -> DemoInputs:
    """Create the deterministic VFR+audio source and Screen Studio-style events."""
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_paths = (
        output_dir / "source-vfr.mp4",
        output_dir / "mousemoves.json",
        output_dir / "mouseclicks.json",
        output_dir / "session.json",
    )
    conflicts = [path.name for path in expected_paths if path.exists()]
    if conflicts:
        raise FileExistsError("refusing to overwrite demo input(s): " + ", ".join(conflicts))

    source_path, moves_path, clicks_path, session_path = expected_paths
    _write_vfr_source(source_path)

    moves = [
        _event("mouseMoved", 1.15, (160.0, 580.0), "arrow"),
        # Same timestamp and coordinate intentionally preserves the shape switch.
        _event("mouseMoved", 1.15, (160.0, 580.0), "pointingHand"),
        _event("mouseMoved", 1.45, (300.0, 180.0), "pointingHand"),
        _event("mouseMoved", 2.80, (1050.0, 190.0), "arrow"),
        _event("mouseMoved", 4.80, (1040.0, 560.0), "arrow"),
        _event("mouseMoved", 6.20, (640.0, 360.0), "arrow"),
    ]
    clicks = [
        _event("mouseDown", 1.50, (300.0, 180.0), "pointingHand", button="left"),
        _event("mouseUp", 1.58, (300.0, 180.0), "pointingHand", button="left"),
        # The down point differs from the unsmoothed move segment and exercises
        # the exact click-anchor rule.
        _event("mouseDown", 2.65, (1040.0, 180.0), "arrow", button="left"),
        _event("mouseUp", 2.72, (1040.0, 180.0), "arrow", button="left"),
        _event("mouseDown", 4.85, (1040.0, 560.0), "arrow", button="left"),
        _event("mouseUp", 4.92, (1040.0, 560.0), "arrow", button="left"),
    ]
    moves_path.write_text(json.dumps(moves, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clicks_path.write_text(json.dumps(clicks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    session_path.write_text(json.dumps(_session_document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in expected_paths
    }
    (output_dir / "input-sha256.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return DemoInputs(output_dir, source_path, session_path, moves_path, clicks_path)


def create_demo(output_dir: Path) -> DemoArtifacts:
    """Create synthetic inputs and render all three supported output formats."""
    output_dir = Path(output_dir).expanduser().resolve()
    rendered_mp4 = output_dir / "sample.mp4"
    rendered_gif = output_dir / "sample.gif"
    rendered_png = output_dir / "sample.png"
    conflicts = [
        path.name for path in (rendered_mp4, rendered_gif, rendered_png) if path.exists()
    ]
    if conflicts:
        raise FileExistsError("refusing to overwrite demo export(s): " + ", ".join(conflicts))

    inputs = create_demo_inputs(output_dir)

    # Keep the public one-command demo on the exact same render and export path as
    # ordinary CLI jobs.  Imports stay local so ``python -m local_demo --help``
    # remains cheap and this module can still generate inputs in isolation.
    from .media import (
        cfr_frame_count,
        cfr_times,
        export_gif,
        export_mp4,
        export_png,
        probe_video,
        rendered_frames,
    )
    from .renderer import Renderer
    from .session import load_session

    session = load_session(inputs.session)
    info = probe_video(inputs.source_video)
    renderer = Renderer(session, (info.width, info.height))
    protected = (
        inputs.source_video,
        inputs.session,
        inputs.mouse_moves,
        inputs.mouse_clicks,
    )

    def mp4_frames() -> Iterable[Image.Image]:
        return rendered_frames(
            info,
            cfr_times(info.duration, session.output_fps),
            renderer.render,
        )

    export_mp4(
        info,
        mp4_frames,
        session.output_fps,
        rendered_mp4,
        protected,
        keep_audio=True,
    )
    gif_fps = 12.0
    gif_start, gif_end = 1.20, 5.40
    def gif_frames() -> Iterable[Image.Image]:
        return rendered_frames(
            info,
            cfr_times(info.duration, gif_fps, gif_start, gif_end),
            renderer.render,
        )

    export_gif(
        info,
        gif_frames,
        gif_fps,
        rendered_gif,
        protected,
        expected_frames=cfr_frame_count(info.duration, gif_fps, gif_start, gif_end),
    )
    png_time = 4.95
    png = next(rendered_frames(info, (png_time,), renderer.render))
    export_png(png, rendered_png, protected)
    return DemoArtifacts(inputs, rendered_mp4, rendered_gif, rendered_png)


__all__ = ["DemoArtifacts", "DemoInputs", "create_demo", "create_demo_inputs"]
