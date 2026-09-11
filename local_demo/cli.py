"""Command-line entry point for the offline renderer."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .media import (
    MediaError,
    cfr_frame_count,
    cfr_times,
    export_gif,
    export_mp4,
    export_png,
    local_input,
    probe_video,
    rendered_frames,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m local_demo",
        description="Render local MP4 + session JSON entirely offline.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a session and inspect its local video")
    validate.add_argument("session", type=Path)

    render = commands.add_parser("render", help="render one MP4, GIF, or exact-time PNG")
    render.add_argument("session", type=Path)
    render.add_argument("output", type=Path, help="new .mp4, .gif, or .png output path")
    render.add_argument("--fps", type=float, help="CFR FPS for MP4; defaults to session outputFps (30)")
    render.add_argument("--no-audio", action="store_true", help="omit the first input audio track from MP4")
    render.add_argument("--gif-start", type=float, help="GIF interval start in seconds, inclusive")
    render.add_argument("--gif-end", type=float, help="GIF interval end in seconds, exclusive")
    render.add_argument("--gif-fps", type=float, help="GIF FPS; defaults to --fps or session outputFps")
    render.add_argument("--at", type=float, help="exact timeline time in seconds for PNG")

    demo = commands.add_parser("demo", help="create and render the fully synthetic example")
    demo.add_argument("--output", required=True, type=Path, help="new directory for generated inputs and exports")
    return parser


def _existing_paths_in_session(session_path: Path, video_path: Path) -> list[Path]:
    """Protect session and referenced local event files from output aliasing."""
    protected = [session_path, video_path]
    try:
        document = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return protected

    def strings(value: object) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    for raw in strings(document):
        if "://" in raw or raw.startswith("file:"):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = session_path.parent / candidate
        try:
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                if resolved not in protected:
                    protected.append(resolved)
        except OSError:
            continue
    return protected


def _load(session_path: Path):
    """Load the core contract only after CLI parsing, so help works without video I/O."""
    from .renderer import Renderer
    from .session import load_session

    session_path = local_input(session_path, "session")
    session = load_session(session_path)
    video_path = local_input(session.video.path, "session video")
    info = probe_video(video_path)
    _validate_session_duration(session, info.duration)
    renderer = Renderer(session, (info.width, info.height))
    return session_path, session, info, renderer, _existing_paths_in_session(session_path, video_path)


def _validate_session_duration(session, duration: float) -> None:
    """Reject events or explicit camera intervals beyond the decodable video."""
    tolerance = 1e-6
    for field_name, events in (("mouseMoves", session.mouse_moves), ("mouseClicks", session.mouse_clicks)):
        for index, event in enumerate(events):
            seconds = (event.process_time_ms - session.process_time_start_ms) / 1000.0
            if seconds > duration + tolerance:
                raise MediaError(
                    f"{field_name}[{index}].processTimeMs is at {seconds:.6f}s, "
                    f"after video duration {duration:.6f}s"
                )
    for index, zoom in enumerate(session.zooms):
        seconds = zoom.end_ms / 1000.0
        if seconds > duration + tolerance:
            raise MediaError(
                f"zooms[{index}].endMs is at {seconds:.6f}s, after video duration {duration:.6f}s"
            )


def _positive(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise MediaError(f"{label} must be a positive finite number")
    return value


def _render(args: argparse.Namespace) -> int:
    session_path, session, info, renderer, protected = _load(args.session)
    suffix = args.output.suffix.lower()
    if suffix not in {".mp4", ".gif", ".png"}:
        raise MediaError("output extension must be .mp4, .gif, or .png")
    if suffix != ".mp4" and args.no_audio:
        raise MediaError("--no-audio only applies to MP4")

    fps_default = args.fps if args.fps is not None else session.output_fps
    if suffix == ".mp4":
        if any(value is not None for value in (args.gif_start, args.gif_end, args.gif_fps, args.at)):
            raise MediaError("GIF/PNG timing options do not apply to MP4")
        fps = _positive(fps_default, "MP4 FPS")

        def frames() -> Iterable:
            return rendered_frames(info, cfr_times(info.duration, fps), renderer.render)

        audio = export_mp4(info, frames, fps, args.output, protected, keep_audio=not args.no_audio)
        print(f"rendered MP4: {args.output} (audio: {audio})")
        return 0

    if suffix == ".gif":
        if args.at is not None:
            raise MediaError("--at only applies to PNG")
        if args.gif_start is None or args.gif_end is None:
            raise MediaError("GIF requires --gif-start and --gif-end")
        fps = _positive(args.gif_fps if args.gif_fps is not None else fps_default, "GIF FPS")
        def frames() -> Iterable:
            return rendered_frames(
                info, cfr_times(info.duration, fps, args.gif_start, args.gif_end), renderer.render
            )

        export_gif(
            info,
            frames,
            fps,
            args.output,
            protected,
            expected_frames=cfr_frame_count(info.duration, fps, args.gif_start, args.gif_end),
        )
        print(f"rendered GIF: {args.output} [{args.gif_start}, {args.gif_end}) at {fps:g} fps")
        return 0

    if args.at is None:
        raise MediaError("PNG requires --at SECONDS")
    if any(value is not None for value in (args.gif_start, args.gif_end, args.gif_fps)):
        raise MediaError("GIF timing options do not apply to PNG")
    if not math.isfinite(args.at) or args.at < 0 or args.at >= info.duration:
        raise MediaError(f"PNG --at must be in [0, {info.duration:.6f})")
    image = next(rendered_frames(info, [args.at], renderer.render))
    export_png(image, args.output, protected)
    print(f"rendered PNG: {args.output} at {args.at:.6f}s")
    return 0


def _validate(args: argparse.Namespace) -> int:
    session_path, session, info, renderer, _protected = _load(args.session)
    print(
        json.dumps(
            {
                "session": str(session_path),
                "video": str(info.path),
                "size": [info.width, info.height],
                "durationSeconds": round(info.duration, 6),
                "outputFps": session.output_fps,
                "audio": info.has_audio,
                "pixelFormat": info.pix_fmt,
                "colorSpace": info.color_space,
                "colorTransfer": info.color_transfer,
                "colorRange": info.color_range,
                "assumedBt709": info.assumed_bt709,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _demo(args: argparse.Namespace) -> int:
    from .demo import create_demo

    create_demo(args.output)
    print(f"created synthetic demo and exports: {args.output}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate(args)
        if args.command == "render":
            return _render(args)
        if args.command == "demo":
            return _demo(args)
        raise AssertionError(f"unknown command {args.command}")
    except (MediaError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
