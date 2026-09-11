"""Local, streaming media I/O used by the CLI.

PyAV owns decoding and timestamp inspection.  The FFmpeg executable distributed by
the pinned ``imageio-ffmpeg`` wheel only owns final encoding; this module never
falls back to an executable on PATH or to another application's files.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence, Tuple

import av
import imageio_ffmpeg
from PIL import Image
from av.video.reformatter import Colorspace


class MediaError(RuntimeError):
    """Raised when a local input cannot be safely rendered or exported."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    duration: float
    first_pts_seconds: float
    has_audio: bool
    audio_start_seconds: Optional[float]
    pix_fmt: str
    color_space: str
    color_transfer: str
    color_range: str
    assumed_bt709: bool


@dataclass(frozen=True)
class DecodedFrame:
    time: float
    image: Image.Image


def ffmpeg_executable() -> Path:
    """Return the pinned wheel's embedded executable without a download fallback."""
    package_dir = Path(imageio_ffmpeg.__file__).resolve().parent
    candidates = sorted((package_dir / "binaries").glob("ffmpeg-*"))
    executable = next((candidate for candidate in candidates if candidate.is_file() and os.access(candidate, os.X_OK)), None)
    if executable is None:
        raise MediaError(
            "imageio-ffmpeg has no bundled FFmpeg executable for this platform; "
            "reinstall the pinned project dependencies."
        )
    return executable


def local_input(path: Path | str, label: str = "input") -> Path:
    """Resolve and validate a plain local file before passing it to a decoder."""
    raw = str(path)
    if "://" in raw or raw.startswith("file:"):
        raise MediaError(f"{label} must be a local file path, not a URL: {raw!r}")
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise MediaError(f"{label} must be a regular file: {resolved}")
    return resolved


def _seconds(value: Optional[int], time_base: Optional[Fraction]) -> Optional[float]:
    if value is None or time_base is None:
        return None
    return float(value * time_base)


def _is_hdr(value: object) -> bool:
    # FFmpeg's transfer enum uses 16 for SMPTE 2084/PQ and 18 for HLG.
    # PyAV 13 exposes that enum as an int, while newer builds may stringify it.
    if isinstance(value, int) and value in {16, 18}:
        return True
    text = str(value).lower()
    return any(marker in text for marker in ("smpte2084", "arib-std-b67", "pq", "hlg"))


def _enum_value(value: object) -> Optional[int]:
    try:
        return int(value)  # PyAV enum values and ints both support this.
    except (TypeError, ValueError):
        return None


def probe_video(path: Path | str) -> VideoInfo:
    """Inspect the first video stream and derive duration from video timestamps only."""
    source = local_input(path, "video")
    try:
        with av.open(str(source), mode="r") as container:
            if not container.streams.video:
                raise MediaError(f"video has no video stream: {source}")
            stream = container.streams.video[0]
            codec = stream.codec_context
            width, height = codec.width, codec.height
            if width <= 0 or height <= 0:
                raise MediaError("video reports an invalid frame size")
            if width % 2 or height % 2:
                raise MediaError(
                    f"first-stage MP4 export needs even dimensions for yuv420p; got {width}x{height}"
                )
            aspect = stream.sample_aspect_ratio
            if aspect is not None and float(aspect) != 1.0:
                raise MediaError(f"non-square pixel aspect ratio is unsupported: {aspect}")
            rotation = stream.metadata.get("rotate")
            if rotation not in (None, "", "0", "0.0"):
                raise MediaError(f"rotated video is unsupported in the first stage: rotate={rotation}")
            if any("displaymatrix" in str(item.type).lower() for item in getattr(stream, "side_data", ())):
                raise MediaError("video stream display-matrix rotation is unsupported in the first stage")
            if _is_hdr(codec.color_trc):
                raise MediaError(f"HDR transfer characteristic is unsupported: {codec.color_trc}")
            video_format = codec.format
            if video_format is None or any(component.bits > 8 for component in video_format.components):
                raise MediaError(f"only 8-bit SDR video is supported; got pixel format {video_format}")
            primaries = _enum_value(codec.color_primaries)
            colorspace = _enum_value(codec.colorspace)
            color_range = _enum_value(codec.color_range)
            if primaries in {9, 10, 11, 12, 22} or colorspace in {9, 10}:
                raise MediaError("wide-gamut video is unsupported in the first stage")
            if colorspace not in {None, 0, 1, 2}:
                raise MediaError(f"unsupported SDR color matrix: {codec.colorspace}")
            if color_range not in {None, 0, 1, 2}:
                raise MediaError(f"unsupported color range: {codec.color_range}")

            first_pts: Optional[float] = None
            last_pts: Optional[float] = None
            last_duration: Optional[float] = None
            previous_pts: Optional[float] = None
            for frame in container.decode(stream):
                frame_seconds = _seconds(frame.pts, frame.time_base)
                if frame_seconds is None:
                    raise MediaError("video has a decoded frame without PTS")
                if previous_pts is not None and frame_seconds < previous_pts:
                    raise MediaError("video decoded PTS are not non-decreasing")
                previous_pts = frame_seconds
                if first_pts is None:
                    first_pts = frame_seconds
                if any("displaymatrix" in str(item.type).lower() for item in getattr(frame, "side_data", ())):
                    raise MediaError("video display-matrix rotation is unsupported in the first stage")
                last_pts = frame_seconds
                frame_duration = _seconds(getattr(frame, "duration", None), frame.time_base)
                if frame_duration is not None and frame_duration > 0:
                    last_duration = frame_duration

            if first_pts is None or last_pts is None:
                raise MediaError("video contains no decodable frames")
            stream_duration = _seconds(stream.duration, stream.time_base)
            if stream_duration is not None and stream_duration > 0:
                duration = stream_duration
            elif last_duration is not None:
                duration = last_pts - first_pts + last_duration
            elif previous_pts is not None and previous_pts > first_pts:
                duration = previous_pts - first_pts
            else:
                raise MediaError("video duration is unavailable from its video stream timestamps")
            if not math.isfinite(duration) or duration <= 0:
                raise MediaError("video has an invalid video-stream duration")
            return VideoInfo(
                path=source,
                width=width,
                height=height,
                duration=duration,
                first_pts_seconds=first_pts,
                has_audio=bool(container.streams.audio),
                audio_start_seconds=(
                    _seconds(container.streams.audio[0].start_time, container.streams.audio[0].time_base)
                    if container.streams.audio
                    else None
                ),
                pix_fmt=str(codec.format),
                color_space=str(codec.colorspace),
                color_transfer=str(codec.color_trc),
                color_range=str(codec.color_range),
                assumed_bt709=colorspace in {None, 0, 2},
            )
    except MediaError:
        raise
    except av.FFmpegError as exc:
        raise MediaError(f"cannot inspect video {source}: {exc}") from exc


def _decoded_frames(info: VideoInfo) -> Iterator[DecodedFrame]:
    """Yield normalized PTS and RGB images, retaining only the decoder's current frame."""
    with av.open(str(info.path), mode="r") as container:
        stream = container.streams.video[0]
        previous_time: Optional[float] = None
        for frame in container.decode(stream):
            timestamp = _seconds(frame.pts, frame.time_base)
            if timestamp is None:
                raise MediaError("video has a decoded frame without PTS")
            normalized = timestamp - info.first_pts_seconds
            if previous_time is not None and normalized < previous_time:
                raise MediaError("video decoded PTS are not non-decreasing")
            previous_time = normalized
            # First-stage input is 8-bit SDR BT.709. If its matrix tag is absent,
            # probe_video records the documented BT.709 assumption. Reformatting
            # explicitly fixes RGB compositing to that matrix; FFmpeg's export
            # scale filter explicitly returns it to limited-range BT.709 YUV.
            rgb = frame.reformat(
                format="rgb24", src_colorspace=Colorspace.ITU709, dst_colorspace=Colorspace.ITU709
            )
            yield DecodedFrame(normalized, rgb.to_image().convert("RGB"))


def cfr_times(duration: float, fps: float, start: float = 0.0, end: Optional[float] = None) -> Iterator[float]:
    """Generate exact-index CFR output times for a half-open time interval."""
    if not math.isfinite(fps) or fps <= 0:
        raise MediaError("output FPS must be a positive finite number")
    if not math.isfinite(start) or start < 0:
        raise MediaError("start time must be a finite non-negative number")
    stop = duration if end is None else end
    if not math.isfinite(stop) or stop <= start or stop > duration + 1e-9:
        raise MediaError(f"time range [{start}, {stop}) is outside video duration {duration:.6f}s")
    frame_count = int(math.ceil((stop - start) * fps - 1e-12))
    for index in range(frame_count):
        timestamp = start + index / fps
        if timestamp < stop:
            yield timestamp


def cfr_frame_count(duration: float, fps: float, start: float = 0.0, end: Optional[float] = None) -> int:
    """Return the frame count for the same half-open schedule as ``cfr_times``."""
    return sum(1 for _ in cfr_times(duration, fps, start, end))


def rendered_frames(
    info: VideoInfo,
    times: Iterable[float],
    render: Callable[[float, Image.Image], Image.Image],
) -> Iterator[Image.Image]:
    """Render scheduled times using the latest decoded source frame at or before each time."""
    decoded = _decoded_frames(info)
    try:
        current = next(decoded)
    except StopIteration as exc:
        raise MediaError("video contains no decodable frames") from exc
    upcoming = next(decoded, None)
    previous_time = -math.inf
    for timestamp in times:
        if timestamp < previous_time:
            raise MediaError("render times must be non-decreasing")
        previous_time = timestamp
        while upcoming is not None and upcoming.time <= timestamp + 1e-12:
            current = upcoming
            upcoming = next(decoded, None)
        output = render(timestamp, current.image)
        if output.mode != "RGB":
            output = output.convert("RGB")
        if output.size != (info.width, info.height):
            raise MediaError(
                f"renderer changed the output size from {info.width}x{info.height} to {output.size[0]}x{output.size[1]}"
            )
        yield output


class AtomicOutput:
    """A same-directory temporary target which only replaces a missing final path on success."""

    def __init__(self, target: Path | str, protected_inputs: Sequence[Path | str] = ()):
        self.target = Path(target).expanduser().resolve(strict=False)
        self.protected_inputs = [Path(item).expanduser().resolve(strict=True) for item in protected_inputs]
        self.temporary: Optional[Path] = None

    def __enter__(self) -> "AtomicOutput":
        if not self.target.suffix:
            raise MediaError("output filename must have an extension")
        if self.target.exists():
            raise MediaError(f"refusing to overwrite existing output: {self.target}")
        for protected in self.protected_inputs:
            if self.target == protected or (self.target.exists() and self.target.samefile(protected)):
                raise MediaError(f"output aliases protected input: {protected}")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.target.stem}.", suffix=f".tmp{self.target.suffix}", dir=self.target.parent
        )
        os.close(fd)
        self.temporary = Path(temp_name)
        return self

    @property
    def path(self) -> Path:
        if self.temporary is None:
            raise RuntimeError("output transaction has not started")
        return self.temporary

    def publish(self) -> None:
        if self.temporary is None:
            raise RuntimeError("output transaction has not started")
        if not self.temporary.exists():
            raise MediaError("encoder did not create its temporary output")
        try:
            # link(2) creates the final directory entry atomically and fails if an
            # equally named entry appeared after our initial check. The temporary
            # and target are deliberately in the same directory/filesystem.
            os.link(self.temporary, self.target)
        except FileExistsError as exc:
            raise MediaError(f"output appeared during export; refusing to overwrite it: {self.target}") from exc
        except OSError as exc:
            raise MediaError(f"cannot atomically publish output: {exc}") from exc
        self.temporary.unlink()
        self.temporary = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)


def _run_encoder(command: Sequence[str], frames: Iterable[Image.Image], progress_label: str) -> None:
    """Stream RGB frames to FFmpeg while sending compact progress to stderr.

    FFmpeg stderr is redirected to a temporary log, preventing its diagnostics from
    filling a pipe when an encode fails.  The log is included in the raised error.
    """
    log_file = tempfile.NamedTemporaryFile(prefix="guideframe-ffmpeg-", suffix=".log", delete=False)
    log_path = Path(log_file.name)
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
        )
        assert process.stdin is not None
        count = 0
        try:
            for image in frames:
                process.stdin.write(image.tobytes())
                count += 1
                if count == 1 or count % 30 == 0:
                    print(f"{progress_label}: rendered {count} frame(s)", file=os.sys.stderr, flush=True)
            process.stdin.close()
            return_code = process.wait()
        except BrokenPipeError as exc:
            try:
                process.stdin.close()
            except OSError:
                pass
            return_code = process.wait()
            log_text = log_path.read_text(errors="replace").strip()
            raise MediaError(
                f"FFmpeg stopped receiving rendered frames ({return_code}): {log_text[-2000:]}"
            ) from exc
        except BaseException:
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
            process.wait()
            raise
        if return_code != 0:
            log_text = log_path.read_text(errors="replace").strip()
            raise MediaError(f"FFmpeg encoding failed ({return_code}): {log_text[-2000:]}")
        if count == 0:
            raise MediaError("renderer produced no frames")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        log_file.close()
        log_path.unlink(missing_ok=True)


def _base_rawvideo_command(info: VideoInfo, fps: float, preserve_timestamps: bool = False) -> list[str]:
    command = [
        str(ffmpeg_executable()),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-protocol_whitelist",
        "file,pipe",
        "-y",
    ]
    if preserve_timestamps:
        command.extend(["-copyts", "-avoid_negative_ts", "disabled"])
    command.extend(
        [
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{info.width}x{info.height}",
        "-framerate",
        f"{fps:.12g}",
        "-i",
        "pipe:0",
        ]
    )
    return command


def verify_output(
    path: Path | str,
    expected_size: Tuple[int, int],
    require_audio: Optional[bool] = None,
    expected_frames: Optional[int] = None,
    expected_duration: Optional[float] = None,
    duration_tolerance: Optional[float] = None,
) -> None:
    """Decode the produced media with PyAV before it becomes a final output."""
    exported = local_input(path, "temporary export")
    try:
        with av.open(str(exported), mode="r") as container:
            if not container.streams.video:
                raise MediaError("export has no video stream")
            stream = container.streams.video[0]
            if (stream.codec_context.width, stream.codec_context.height) != expected_size:
                raise MediaError("export dimensions differ from the render dimensions")
            frame_count = sum(1 for _ in container.decode(stream))
            if frame_count == 0:
                raise MediaError("export contains no decodable video frames")
            if expected_frames is not None and frame_count != expected_frames:
                raise MediaError(f"export decoded {frame_count} video frames, expected {expected_frames}")
            if expected_duration is not None:
                actual_duration = _seconds(stream.duration, stream.time_base)
                tolerance = duration_tolerance if duration_tolerance is not None else 0.050
                if actual_duration is None or abs(actual_duration - expected_duration) > tolerance:
                    raise MediaError(
                        f"export video duration {actual_duration!r}s differs from expected {expected_duration:.6f}s"
                    )
            if require_audio is True and not container.streams.audio:
                raise MediaError("export unexpectedly lost its first audio track")
    except MediaError:
        raise
    except av.FFmpegError as exc:
        raise MediaError(f"cannot verify exported media: {exc}") from exc


def export_mp4(
    info: VideoInfo,
    make_frames: Callable[[], Iterable[Image.Image]],
    fps: float,
    target: Path | str,
    protected_inputs: Sequence[Path | str],
    keep_audio: bool = True,
) -> str:
    """Encode streamed rendered frames as CFR H.264 and retain the first audio track when present."""
    with AtomicOutput(target, protected_inputs) as transaction:
        temporary = transaction.path
        audio_mode = "none"
        copy_audio = (
            keep_audio
            and info.has_audio
            and info.audio_start_seconds is not None
            and info.audio_start_seconds >= info.first_pts_seconds - 1e-9
        )
        command = _base_rawvideo_command(info, fps, preserve_timestamps=keep_audio and info.has_audio)
        if keep_audio and info.has_audio:
            # The raw rendered video starts at normalized video t=0. Shift the
            # original container so audio keeps its offset from the first video
            # PTS instead of its offset from the container's arbitrary origin.
            command.extend(
                [
                    "-itsoffset",
                    f"{-info.first_pts_seconds:.12g}",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(info.path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0?",
                ]
            )
            audio_mode = "copy" if copy_audio else "aac"
        else:
            command.extend(["-map", "0:v:0"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-colorspace",
                "bt709",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-x264-params",
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off",
                "-vf",
                "scale=out_color_matrix=bt709:out_range=tv",
            ]
        )
        if audio_mode == "copy":
            command.extend(["-c:a", "copy"])
        elif audio_mode == "aac":
            command.extend(["-c:a", "aac", "-af", f"atrim=start=0:end={info.duration:.12g}"])
        command.extend(["-t", f"{info.duration:.12g}", "-movflags", "+faststart", str(temporary)])
        try:
            _run_encoder(command, make_frames(), "MP4")
            verify_output(
                temporary,
                (info.width, info.height),
                require_audio=info.has_audio and keep_audio,
                expected_frames=cfr_frame_count(info.duration, fps),
                expected_duration=info.duration,
                duration_tolerance=1.0 / fps + 0.002,
            )
        except MediaError as copy_error:
            if audio_mode != "copy":
                raise
            # Remuxing a valid source track can fail for an incompatible codec. Retry
            # with AAC without changing the final-path atomicity guarantee.
            command[command.index("copy", command.index("-c:a"))] = "aac"
            audio_codec_index = command.index("aac", command.index("-c:a"))
            command[audio_codec_index + 1 : audio_codec_index + 1] = [
                "-af",
                f"atrim=start=0:end={info.duration:.12g}",
            ]
            try:
                _run_encoder(command, make_frames(), "MP4 (AAC fallback)")
                verify_output(
                    temporary,
                    (info.width, info.height),
                    require_audio=True,
                    expected_frames=cfr_frame_count(info.duration, fps),
                    expected_duration=info.duration,
                    duration_tolerance=1.0 / fps + 0.002,
                )
                audio_mode = "aac"
            except BaseException as fallback_error:
                raise MediaError(f"audio copy failed ({copy_error}); AAC fallback failed: {fallback_error}") from fallback_error
        transaction.publish()
        return audio_mode


def export_gif(
    info: VideoInfo,
    make_frames: Callable[[], Iterable[Image.Image]],
    fps: float,
    target: Path | str,
    protected_inputs: Sequence[Path | str],
    expected_frames: Optional[int] = None,
) -> None:
    """Render twice: one stream builds a palette, the other uses it for GIF."""
    with AtomicOutput(target, protected_inputs) as transaction:
        temporary = transaction.path
        palette_handle = tempfile.NamedTemporaryFile(
            prefix=f".{transaction.target.stem}.", suffix=".palette.png", dir=transaction.target.parent, delete=False
        )
        palette = Path(palette_handle.name)
        palette_handle.close()
        try:
            palette_command = _base_rawvideo_command(info, fps) + [
                "-vf",
                "palettegen=stats_mode=full",
                "-frames:v",
                "1",
                str(palette),
            ]
            _run_encoder(palette_command, make_frames(), "GIF palette")
            command = _base_rawvideo_command(info, fps) + [
                "-i",
                str(palette),
                "-lavfi",
                "[0:v][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
                "-loop",
                "0",
                str(temporary),
            ]
            _run_encoder(command, make_frames(), "GIF")
            verify_output(temporary, (info.width, info.height), expected_frames=expected_frames)
        finally:
            palette.unlink(missing_ok=True)
        transaction.publish()


def export_png(
    image: Image.Image,
    target: Path | str,
    protected_inputs: Sequence[Path | str],
) -> None:
    """Write one already-rendered exact-time RGB image transactionally."""
    with AtomicOutput(target, protected_inputs) as transaction:
        temporary = transaction.path
        try:
            image.save(temporary, format="PNG")
            with Image.open(temporary) as verified:
                verified.verify()
        except OSError as exc:
            raise MediaError(f"cannot write PNG: {exc}") from exc
        transaction.publish()
