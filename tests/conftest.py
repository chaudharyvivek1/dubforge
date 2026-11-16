import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dubforge import ffmpeg  # noqa: E402
from dubforge.config import Settings  # noqa: E402


def _have_ffmpeg() -> bool:
    try:
        ffmpeg.ffmpeg_path()
        return True
    except ffmpeg.FFmpegMissing:
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="no ffmpeg available")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(workdir=str(tmp_path / "work"), cache=False)


@pytest.fixture()
def workdir(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def demo_clip(tmp_path):
    from dubforge import demo

    if not _have_ffmpeg():
        pytest.skip("no ffmpeg available")
    return demo.build_clip(tmp_path / "clip.mp4")
