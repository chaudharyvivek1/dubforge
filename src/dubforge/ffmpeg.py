"""Everything that touches samples or pixels goes through here.

The rest of the package never builds a filter graph by hand: it calls one of the
functions below. ffmpeg is resolved in this order, and `dubforge doctor` prints
which one won:

1. ``$DUBFORGE_FFMPEG``
2. ``ffmpeg`` on PATH
3. the static binary shipped inside ``imageio-ffmpeg``

There is no ffprobe dependency. Probing parses ``ffmpeg -i`` banner output, which
is stable enough for duration, resolution, fps and audio layout, and means a
minimal install (one binary) is enough to dub a video.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    """ffmpeg was found but refused the job. The message carries its stderr tail."""


class FFmpegMissing(RuntimeError):
    """No usable ffmpeg binary."""


_CACHE: dict[str, str] = {}


def ffmpeg_path() -> str:
    if "ffmpeg" in _CACHE:
        return _CACHE["ffmpeg"]
    env = os.environ.get("DUBFORGE_FFMPEG")
    if env:
        if not Path(env).exists():
            raise FFmpegMissing(f"DUBFORGE_FFMPEG points at {env}, which does not exist")
        _CACHE["ffmpeg"] = env
        return env
    found = shutil.which("ffmpeg")
    if found:
        _CACHE["ffmpeg"] = found
        return found
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            _CACHE["ffmpeg"] = bundled
            return bundled
    except Exception:  # noqa: BLE001 - optional dependency, any failure means "not there"
        pass
    raise FFmpegMissing(
        "no ffmpeg. Install one of:\n"
        "  Debian/Ubuntu : sudo apt install ffmpeg\n"
        "  macOS         : brew install ffmpeg\n"
        "  Windows       : winget install Gyan.FFmpeg\n"
        "  any platform  : pip install imageio-ffmpeg\n"
        "then re-run, or point DUBFORGE_FFMPEG at the binary."
    )


def ffmpeg_source() -> str:
    """Human-readable description of where the binary came from (for `doctor`)."""
    try:
        path = ffmpeg_path()
    except FFmpegMissing:
        return "missing"
    if os.environ.get("DUBFORGE_FFMPEG") == path:
        return "DUBFORGE_FFMPEG"
    if shutil.which("ffmpeg") == path:
        return "system PATH"
    return "imageio-ffmpeg bundle"


def version() -> str:
    try:
        out = subprocess.run(
            [ffmpeg_path(), "-version"], capture_output=True, text=True, timeout=30
        )
    except (FFmpegMissing, OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    first = (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else ""
    return first.strip()


def run(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run ffmpeg, raise FFmpegError with the last stderr lines on failure."""
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s: {' '.join(args[:6])} …") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}) on {' '.join(args[:6])} …\n{tail}")
    return proc


# --------------------------------------------------------------------------- probe


@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def summary(self) -> str:
        bits = [f"{self.duration:.2f}s"]
        if self.has_video:
            bits.append(f"{self.width}x{self.height}@{self.fps:g} {self.video_codec}")
        if self.has_audio:
            bits.append(f"{self.audio_codec} {self.sample_rate}Hz {self.channels}ch")
        return " · ".join(bits)


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?:\s*(Video|Audio):\s*([A-Za-z0-9_]+)(.*)")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"([\d.]+)\s*fps")
_RATE_RE = re.compile(r"(\d+)\s*Hz")
_CH_RE = re.compile(r"(mono|stereo|[\d.]+\(.*?\))", re.IGNORECASE)


def probe(path: str | Path) -> MediaInfo:
    """Read duration and stream layout. Raises FFmpegError if the file is unreadable."""
    path = str(path)
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", "-i", path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    if "No such file" in text or not text.strip():
        raise FFmpegError(f"cannot read {path}")
    info = MediaInfo(path=path)
    m = _DUR_RE.search(text)
    if m:
        h, mi, s = m.groups()
        info.duration = int(h) * 3600 + int(mi) * 60 + float(s)
    for kind, codec, rest in _STREAM_RE.findall(text):
        if kind == "Video" and not info.has_video:
            info.has_video = True
            info.video_codec = codec
            size = _SIZE_RE.search(rest)
            if size:
                info.width, info.height = int(size.group(1)), int(size.group(2))
            fps = _FPS_RE.search(rest)
            if fps:
                info.fps = float(fps.group(1))
        elif kind == "Audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = codec
            rate = _RATE_RE.search(rest)
            if rate:
                info.sample_rate = int(rate.group(1))
            ch = _CH_RE.search(rest)
            if ch:
                token = ch.group(1).lower()
                info.channels = 1 if token == "mono" else 2 if token == "stereo" else 2
    if not (info.has_audio or info.has_video):
        raise FFmpegError(f"{path} has no audio or video stream")
    return info


def duration_of(path: str | Path) -> float:
    return probe(path).duration


# --------------------------------------------------------------------------- audio in


def extract_audio(
    src: str | Path,
    dst: str | Path,
    sample_rate: int = 16000,
    channels: int = 1,
    start: float | None = None,
    length: float | None = None,
) -> str:
    """Decode the audio track to a plain PCM wav (what every ASR provider wants)."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    args += [
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    run(args)
    return str(dst)


def split_channels(src: str | Path, out_dir: str | Path) -> list[str]:
    """Split an interleaved stereo file into per-channel wavs (crude centre isolation)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    n = max(1, info.channels)
    paths = []
    for ch in range(n):
        dst = out_dir / f"ch{ch}.wav"
        run(["-i", str(src), "-af", f"pan=mono|c0=c{ch}", str(dst)])
        paths.append(str(dst))
    return paths


# --------------------------------------------------------------------------- audio ops


def atempo_chain(factor: float) -> str:
    """atempo filter string for any positive factor (atempo alone is limited to 0.5–2)."""
    if factor <= 0:
        raise ValueError("speed factor must be > 0")
    parts: list[str] = []
    f = float(factor)
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


def stretch(src: str | Path, dst: str | Path, factor: float) -> str:
    """Change duration by `factor` without shifting pitch (factor>1 = faster/shorter).

    Stretching a file in place is the normal case in the pipeline, and ffmpeg cannot
    read and write the same path — so it goes through a sibling temp file.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        temp = dst.with_name(f"{dst.stem}.stretch{dst.suffix}")
        run(["-i", str(src), "-filter:a", atempo_chain(factor), str(temp)])
        os.replace(temp, dst)
        return str(dst)
    run(["-i", str(src), "-filter:a", atempo_chain(factor), str(dst)])
    return str(dst)


def silence_spans(
    src: str | Path, noise_db: int = -35, min_duration: float = 0.35
) -> list[tuple[float, float]]:
    """Silence intervals, from ffmpeg's silencedetect. Used for honest segmentation."""
    proc = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].strip().split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0])
            except (IndexError, ValueError):
                continue
            spans.append((start, end))
            start = None
    if start is not None:
        # ffmpeg only emits silence_end on the transition back to sound, so a
        # silence that runs to the end of the file never closes. Close it here.
        with contextlib.suppress(FFmpegError):
            spans.append((start, duration_of(src)))
    return spans


def loudnorm(
    src: str | Path, dst: str | Path, target_lufs: float = -16.0, true_peak: float = -1.5
) -> str:
    """Single-pass EBU R128 normalisation to a streaming-friendly target."""
    run(
        [
            "-i",
            str(src),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
            "-ar",
            "48000",
            str(dst),
        ]
    )
    return str(dst)


def measure_loudness(src: str | Path) -> dict:
    """Integrated loudness / true peak, parsed from loudnorm's JSON report."""
    proc = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    text = proc.stderr or ""
    start, end = text.rfind("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for key, name in (
        ("input_i", "integrated_lufs"),
        ("input_tp", "true_peak_db"),
        ("input_lra", "lra"),
        ("input_thresh", "threshold"),
    ):
        with contextlib.suppress(KeyError, TypeError, ValueError):
            out[name] = float(raw[key])
    return out


def peak_db(src: str | Path) -> float | None:
    proc = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(src),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", proc.stderr or "")
    return float(m.group(1)) if m else None


def silence(dst: str | Path, seconds: float, sample_rate: int = 48000) -> str:
    run(
        [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=stereo",
            "-t",
            f"{seconds:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    return str(dst)


def assemble_dub(
    clips: list[tuple[str | Path, float]],
    dst: str | Path,
    total_duration: float,
    sample_rate: int = 48000,
) -> str:
    """Lay timed clips onto one silent track, preserving every start time.

    Clips are mixed, never concatenated, so a segment that runs long overlaps the
    gap after it instead of pushing every later line out of sync.
    """
    dst = Path(dst)
    if not clips:
        return silence(dst, max(total_duration, 0.1), sample_rate)

    chunk_size = 48  # amix gets slow and the filter string gets long past this
    chunks: list[list[tuple[str, float]]] = [
        list(clips[i : i + chunk_size]) for i in range(0, len(clips), chunk_size)
    ]
    partials: list[str] = []
    for ci, chunk in enumerate(chunks):
        inputs: list[str] = []
        for path, _ in chunk:
            inputs += ["-i", str(path)]
        chains = []
        labels = []
        for i, (_, start) in enumerate(chunk):
            ms = max(0, int(round(start * 1000)))
            chains.append(
                f"[{i}:a]aresample={sample_rate},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"adelay={ms}:all=1[s{i}]"
            )
            labels.append(f"[s{i}]")
        graph = ";".join(chains) + ";" + "".join(labels)
        graph += f"amix=inputs={len(chunk)}:normalize=0:dropout_transition=0"
        out = dst.with_name(f"{dst.stem}.part{ci}.wav")
        args = [*inputs, "-filter_complex"]
        if len(chunks) == 1:
            # a single pass has to cover the whole runtime, or the mux -shortest
            # will trim the picture to the last line of dialogue
            args += [graph + ",apad[mix]", "-map", "[mix]", "-t", f"{total_duration:.3f}"]
        else:
            args += [graph + "[mix]", "-map", "[mix]"]
        args += ["-c:a", "pcm_s16le", str(out)]
        run(args)
        partials.append(str(out))

    if len(partials) == 1:
        shutil.move(partials[0], dst)
        return str(dst)

    inputs = []
    for p in partials:
        inputs += ["-i", p]
    graph = "".join(f"[{i}:a]" for i in range(len(partials)))
    graph += f"amix=inputs={len(partials)}:normalize=0:dropout_transition=0,apad[mix]"
    run(
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[mix]",
            "-t",
            f"{total_duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    for p in partials:
        Path(p).unlink(missing_ok=True)
    return str(dst)


def duck_and_mix(
    voice: str | Path,
    bed: str | Path,
    dst: str | Path,
    voice_gain_db: float = 0.0,
    bed_gain_db: float = -16.0,
    duck: bool = True,
    total_duration: float | None = None,
) -> str:
    """Mix the dub over the original music/effects, ducking it under speech.

    This is what separates a dub from a re-voiced karaoke track: the bed keeps the
    laugh, the door slam and the score, and dips only while someone is talking.
    """
    graph = (
        f"[0:a]volume={voice_gain_db}dB,aresample=48000,aformat=channel_layouts=stereo,asplit=2[v1][v2];"
        f"[1:a]volume={bed_gain_db}dB,aresample=48000,aformat=channel_layouts=stereo[b];"
    )
    if duck:
        graph += (
            "[b][v1]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=320:makeup=1[bd];"
            "[v2][bd]amix=inputs=2:normalize=0[mix]"
        )
    else:
        graph += "[v2][b]amix=inputs=2:normalize=0[mix]"
    args = ["-i", str(voice), "-i", str(bed), "-filter_complex", graph, "-map", "[mix]"]
    if total_duration:
        args += ["-t", f"{total_duration:.3f}"]
    args += ["-c:a", "pcm_s16le", str(dst)]
    run(args)
    return str(dst)


def mux(
    video: str | Path,
    audio: str | Path,
    dst: str | Path,
    audio_bitrate: str = "192k",
    shortest: bool = True,
) -> str:
    """Put the dub back on the picture. Video is stream-copied: no re-encode, no quality loss."""
    args = [
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
    ]
    if shortest:
        args += ["-shortest"]
    args += [str(dst)]
    run(args)
    return str(dst)


def mux_audio_only(audio: str | Path, dst: str | Path, bitrate: str = "192k") -> str:
    """Audio-only source: encode the dub to m4a instead of trying to mux a picture."""
    run(["-i", str(audio), "-c:a", "aac", "-b:a", bitrate, "-ar", "48000", str(dst)])
    return str(dst)


def write_tone_sequence(
    dst: str | Path,
    bursts: list[tuple[float, float, float]],
    total: float,
    sample_rate: int = 48000,
) -> str:
    """Build a clip of tone bursts at given (start, length, frequency).

    Used by `dubforge demo` to make deterministic, speech-shaped audio with real
    silences, so the offline pipeline exercises the same code as a real video.
    """
    inputs: list[str] = []
    for _start, length, freq in bursts:
        inputs += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate={sample_rate}:duration={length:.3f}",
        ]
    chains = []
    labels = []
    for i, (start, _length, _freq) in enumerate(bursts):
        chains.append(
            f"[{i}:a]aresample={sample_rate},aformat=channel_layouts=stereo,"
            # ffmpeg's sine source peaks at -18 dBFS and the stereo conversion costs
            # another 3 dB, so a gain is needed for the burst to be audible at all.
            f"volume=2.5,adelay={int(start * 1000)}:all=1[s{i}]"
        )
        labels.append(f"[s{i}]")
    graph = ";".join(chains) + ";" + "".join(labels)
    graph += f"amix=inputs={len(bursts)}:normalize=0,apad[mix]"
    run(
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[mix]",
            "-t",
            f"{total:.3f}",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    return str(dst)
