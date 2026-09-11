from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import av
import numpy as np
from PIL import Image
import pytest

from local_demo.cli import main
from local_demo.media import (
    MediaError,
    cfr_times,
    export_mp4,
    ffmpeg_executable,
    probe_video,
    rendered_frames,
)
from local_demo.renderer import Renderer
from local_demo.session import Bounds, Session, VideoSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = PROJECT_ROOT / "examples" / "validated_demo"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_video(path: Path):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        frames = list(container.decode(stream))
        duration = float(stream.duration * stream.time_base)
        return stream, frames, duration, len(container.streams.audio)


def _source_session(source: Path, info) -> Session:
    return Session(
        source_path=source.parent / "session.json",
        video=VideoSpec(source, info.width, info.height),
        bounds=Bounds(0, 0, info.width, info.height),
        process_time_start_ms=0,
        mouse_moves=(),
        mouse_clicks=(),
        zooms=(),
        output_fps=30,
    )


def test_checked_in_synthetic_artifacts_cover_vfr_cfr_audio_gif_png_and_hashes():
    manifest = json.loads((DEMO_DIR / "input-sha256.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        assert _sha256(DEMO_DIR / name) == digest

    source_stream, source_frames, source_duration, source_audio = _decode_video(
        DEMO_DIR / "source-vfr.mp4"
    )
    output_stream, output_frames, output_duration, output_audio = _decode_video(
        DEMO_DIR / "sample.mp4"
    )
    source_pts = [float(frame.time) for frame in source_frames]
    output_pts = [float(frame.time) for frame in output_frames]

    assert source_stream.codec_context.width == output_stream.codec_context.width == 1280
    assert source_stream.codec_context.height == output_stream.codec_context.height == 720
    assert len(source_frames) == 7
    assert max(b - a for a, b in zip(source_pts, source_pts[1:])) > 1.0
    assert source_duration == pytest.approx(7.0, abs=0.002)
    assert len(output_frames) == 210
    assert output_duration == pytest.approx(7.0, abs=0.002)
    assert np.diff(output_pts) == pytest.approx([1 / 30] * 209, abs=1e-8)
    assert source_audio == output_audio == 1
    assert output_stream.codec_context.name == "h264"
    assert output_stream.codec_context.format.name == "yuv420p"
    assert int(output_stream.codec_context.colorspace) == 1
    assert int(output_stream.codec_context.color_primaries) == 1
    assert int(output_stream.codec_context.color_trc) == 1
    assert int(output_stream.codec_context.color_range) == 1

    with Image.open(DEMO_DIR / "sample.gif") as gif:
        durations = []
        assert gif.size == (1280, 720)
        assert gif.n_frames == 51
        assert gif.info.get("loop") == 0
        for index in range(gif.n_frames):
            gif.seek(index)
            durations.append(gif.info["duration"])
        assert set(durations) <= {80, 90}
        assert sum(durations) == pytest.approx(4250, abs=10)

    with Image.open(DEMO_DIR / "sample.png") as png:
        png.verify()
    with Image.open(DEMO_DIR / "sample.png") as png:
        assert png.size == (1280, 720)


def test_vfr_held_source_still_has_smooth_cfr_cursor_motion_in_encoded_output():
    with av.open(str(DEMO_DIR / "sample.mp4")) as container:
        frames = {
            round(float(frame.time), 6): np.asarray(frame.to_image().convert("RGB"), dtype=np.int16)
            for frame in container.decode(container.streams.video[0])
            if 1.92 <= float(frame.time) <= 2.01
        }
    first = frames[round(58 / 30, 6)]
    second = frames[2.0]
    difference = np.max(np.abs(first - second), axis=2)

    # There is no source PTS between 1.92 and 3.32. Significant changes in this
    # short pair are localized around the moving rendered pointer, while the
    # held UI background remains nearly identical after H.264 round trips.
    assert np.count_nonzero(difference > 10) > 500
    assert float(np.mean(difference)) < 1.0


def test_no_effect_first_frame_preserves_sampled_ui_color_patches():
    with av.open(str(DEMO_DIR / "source-vfr.mp4")) as container:
        source = np.asarray(next(container.decode(container.streams.video[0])).to_image(), dtype=np.int16)
    with av.open(str(DEMO_DIR / "sample.mp4")) as container:
        output = np.asarray(next(container.decode(container.streams.video[0])).to_image(), dtype=np.int16)

    # Solid UI patches are independent of text antialiasing and pointer effects.
    for x, y in ((10, 10), (50, 110), (400, 300), (600, 300), (900, 300), (500, 500)):
        assert np.max(np.abs(source[y, x] - output[y, x])) <= 4


@pytest.mark.parametrize("kind", ["mouse", "zoom"])
def test_cli_rejects_timeline_data_after_video_end(tmp_path: Path, capsys, kind: str):
    document = json.loads((DEMO_DIR / "session.json").read_text(encoding="utf-8"))
    document["video"]["path"] = str((DEMO_DIR / "source-vfr.mp4").resolve())
    document["mouseMoves"] = json.loads((DEMO_DIR / "mousemoves.json").read_text(encoding="utf-8"))
    document["mouseClicks"] = json.loads((DEMO_DIR / "mouseclicks.json").read_text(encoding="utf-8"))
    if kind == "mouse":
        document["mouseMoves"].append(
            {
                "activeModifiers": [],
                "cursorId": "arrow",
                "processTimeMs": 8100,
                "type": "mouseMoved",
                "unixTimeMs": 1_800_000_008_100,
                "x": 420,
                "y": 230,
            }
        )
    else:
        document["zooms"][-1]["endMs"] = 7100
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(document), encoding="utf-8")

    assert main(["validate", str(session_path)]) == 2
    assert "after video duration" in capsys.readouterr().err


def _make_offset_source(path: Path, *, video_start: float, audio_start: float) -> None:
    command = [str(ffmpeg_executable()), "-hide_banner", "-loglevel", "error"]
    if video_start:
        command.extend(["-itsoffset", str(video_start)])
    command.extend(["-f", "lavfi", "-i", "color=c=navy:s=64x64:r=30:d=1"])
    if audio_start:
        command.extend(["-itsoffset", str(audio_start)])
    command.extend(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1.5",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-copyts",
            "-avoid_negative_ts",
            "disabled",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ]
    )
    subprocess.run(command, check=True)


@pytest.mark.parametrize(
    ("video_start", "audio_start", "expected_mode", "expected_audio_start"),
    [(0.0, 0.20, "copy", 0.20), (1.0, 0.0, "aac", 0.0)],
)
def test_audio_offset_is_preserved_or_cropped_without_shortening_video(
    tmp_path: Path,
    video_start: float,
    audio_start: float,
    expected_mode: str,
    expected_audio_start: float,
):
    source = tmp_path / "source.mp4"
    _make_offset_source(source, video_start=video_start, audio_start=audio_start)
    info = probe_video(source)
    session = _source_session(source, info)
    renderer = Renderer(session, (info.width, info.height))
    target = tmp_path / "rendered.mp4"

    def frames():
        return rendered_frames(info, cfr_times(info.duration, 30), renderer.render)

    mode = export_mp4(info, frames, 30, target, (source,), keep_audio=True)
    assert mode == expected_mode
    with av.open(str(target)) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        video_duration = float(video.duration * video.time_base)
        audio_start_result = float(audio.start_time * audio.time_base)
        assert sum(1 for _ in container.decode(video)) == round(info.duration * 30)
        assert video_duration == pytest.approx(info.duration, abs=1 / 30 + 0.002)
        assert audio_start_result == pytest.approx(expected_audio_start, abs=0.025)


@pytest.mark.parametrize(
    ("pixel_format", "transfer", "message"),
    [("yuv420p10le", "bt709", "only 8-bit"), ("yuv420p", "smpte2084", "HDR")],
)
def test_probe_rejects_10_bit_and_hdr_sources(tmp_path: Path, pixel_format, transfer, message):
    source = tmp_path / "unsupported.mkv"
    codec_options = (
        ["-c:v", "libx264", "-pix_fmt", pixel_format, "-color_trc", transfer,
         "-x264-params", "transfer=smpte2084:colorprim=bt709:colormatrix=bt709"]
        if transfer == "smpte2084"
        else ["-c:v", "ffv1", "-pix_fmt", pixel_format, "-color_trc", transfer]
    )
    subprocess.run(
        [
            str(ffmpeg_executable()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=5:d=0.4",
            *codec_options,
            "-y",
            str(source),
        ],
        check=True,
    )
    with pytest.raises(MediaError, match=message):
        probe_video(source)
