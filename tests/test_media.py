from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from local_demo import media
from local_demo.media import AtomicOutput, DecodedFrame, MediaError, VideoInfo, cfr_times, rendered_frames


def test_cfr_times_are_index_based_and_half_open():
    assert list(cfr_times(2.0, 4.0, 0.5, 1.51)) == pytest.approx(
        [0.5, 0.75, 1.0, 1.25, 1.5]
    )


def test_vfr_source_holds_latest_pts_while_effect_time_keeps_advancing(monkeypatch, tmp_path: Path):
    red = Image.new("RGB", (2, 2), "red")
    blue = Image.new("RGB", (2, 2), "blue")
    info = VideoInfo(
        path=tmp_path / "source.mp4",
        width=2,
        height=2,
        duration=2.0,
        first_pts_seconds=0.0,
        has_audio=False,
        audio_start_seconds=None,
        pix_fmt="yuv420p",
        color_space="1",
        color_transfer="1",
        color_range="1",
        assumed_bt709=False,
    )

    def decoded(_info):
        yield DecodedFrame(0.0, red)
        yield DecodedFrame(1.3, blue)

    monkeypatch.setattr(media, "_decoded_frames", decoded)
    observations = []

    def render(t, source):
        observations.append((t, source.getpixel((0, 0))))
        output = source.copy()
        output.putpixel((1, 1), (round(t * 100), 0, 0))
        return output

    frames = list(rendered_frames(info, (0.0, 0.4, 0.8, 1.2, 1.6), render))
    assert [color for _, color in observations] == [
        (255, 0, 0),
        (255, 0, 0),
        (255, 0, 0),
        (255, 0, 0),
        (0, 0, 255),
    ]
    assert [frame.getpixel((1, 1))[0] for frame in frames] == [0, 40, 80, 120, 160]


def test_atomic_output_refuses_overwrite_and_cleans_failed_temporary(tmp_path: Path):
    existing = tmp_path / "result.png"
    existing.write_bytes(b"keep me")
    with pytest.raises(MediaError, match="refusing to overwrite"):
        with AtomicOutput(existing):
            pass
    assert existing.read_bytes() == b"keep me"

    target = tmp_path / "failed.png"
    temporary_path = None
    with pytest.raises(RuntimeError, match="planned failure"):
        with AtomicOutput(target) as transaction:
            temporary_path = transaction.path
            transaction.path.write_bytes(b"partial")
            raise RuntimeError("planned failure")
    assert not target.exists()
    assert temporary_path is not None
    assert not temporary_path.exists()
