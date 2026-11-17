"""Batch dubbing: many files, one pipeline, honest per-file results.

There is no second dubbing implementation here. A batch submits jobs to the same
`JobRunner`, which runs the same `pipeline.dub`, and collects the outcomes — so a fix in
the pipeline is a fix in batch mode, and a batch is exactly as trustworthy as a single
run.

The rules that matter in a school lab: one broken video must not stop the others, a
finished video must not be dubbed twice, and nothing may overwrite its own source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import pipeline
from .config import Settings
from .jobs import CANCELLED, COMPLETED, FAILED, INTERRUPTED, JobRunner, JobStore

MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".avi",
    ".mpeg",
    ".mpg",
    ".ts",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
}


@dataclass
class BatchItem:
    source: str
    job_id: str = ""
    status: str = "pending"
    qc_ok: bool | None = None
    output: str = ""
    error: str = ""
    duration_s: float | None = None
    skipped: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class BatchResult:
    items: list[BatchItem] = field(default_factory=list)
    estimates: dict = field(default_factory=dict)

    @property
    def counts(self) -> dict:
        counts = {
            "total": len(self.items),
            "completed": 0,
            "qc_failed": 0,
            "failed": 0,
            "cancelled": 0,
            "interrupted": 0,
            "skipped": 0,
        }
        for item in self.items:
            if item.skipped:
                counts["skipped"] += 1
            elif item.status == COMPLETED and item.qc_ok:
                counts["completed"] += 1
            elif item.status == COMPLETED:
                counts["qc_failed"] += 1
            elif item.status == CANCELLED:
                counts["cancelled"] += 1
            elif item.status == INTERRUPTED:
                counts["interrupted"] += 1
            else:
                counts["failed"] += 1
        return counts

    @property
    def ok(self) -> bool:
        """A batch is clean only if every file finished and every one passed QC."""
        counts = self.counts
        return counts["failed"] == 0 and counts["cancelled"] == 0 and counts["qc_failed"] == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "counts": self.counts,
            "estimates": self.estimates,
            "items": [item.to_dict() for item in self.items],
        }

    def table(self) -> str:
        lines = [f"{'file':<40} {'status':<11} {'qc':<5} {'time':>7}"]
        for item in self.items:
            qc = "—" if item.qc_ok is None else ("pass" if item.qc_ok else "FAIL")
            secs = f"{item.duration_s:.0f}s" if item.duration_s else "—"
            name = Path(item.source).name[:38]
            status = "skipped" if item.skipped else item.status
            lines.append(f"{name:<40} {status:<11} {qc:<5} {secs:>7}")
            if item.error:
                lines.append(f"    {item.error[:100]}")
        counts = self.counts
        lines.append("")
        lines.append(
            f"{counts['total']} file(s): {counts['completed']} completed, "
            f"{counts['qc_failed']} failed QC, {counts['failed']} failed, "
            f"{counts['skipped']} skipped"
        )
        return "\n".join(lines)


def known_outputs(store: JobStore) -> set[str]:
    """Every output path any job has produced, so a batch never dubs its own results."""
    outputs: set[str] = set()
    for job in store.list(limit=200)[0]:
        if job.output:
            outputs.add(str(Path(job.output).resolve()))
    return outputs


def collect_sources(
    target: str | Path,
    recursive: bool = True,
    exclude_dir: str | Path | None = None,
    store: JobStore | None = None,
) -> list[Path]:
    """A file, or a folder of media files.

    `exclude_dir` keeps a batch from feeding on its own output: dubbing writes into
    `<workdir>/out/`, and a second run must not treat those files as new lessons.
    """
    path = Path(target).expanduser()
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"no such file or folder: {target}")
    skip = Path(exclude_dir).expanduser().resolve() if exclude_dir else None
    outputs = known_outputs(store) if store is not None else set()
    pattern = "**/*" if recursive else "*"
    found = []
    for candidate in path.glob(pattern):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if str(candidate.resolve()) in outputs:
            continue  # this file is a dub this tool produced, not a lesson
        if skip is not None:
            try:
                candidate.resolve().relative_to(skip)
                continue  # inside the work directory: not an input
            except ValueError:
                pass
        found.append(candidate)
    return sorted(found)


def output_for(source: str | Path, settings: Settings, out_dir: str | Path | None = None) -> Path:
    """Where a batch writes a dub: outside the input folder, never over the source."""
    source = Path(source)
    audio_only = source.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
    suffix = ".m4a" if audio_only else ".mp4"
    stem = source.stem
    if stem.endswith(f".{settings.target_lang}"):
        stem = stem[: -(len(settings.target_lang) + 1)]  # already tagged: do not tag twice
    directory = Path(out_dir) if out_dir else Path(settings.workdir) / "out"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}.{settings.target_lang}{suffix}"


def run_batch(
    sources: list[str | Path],
    settings: Settings | None = None,
    runner: JobRunner | None = None,
    force: bool = False,
    stop_on_error: bool = False,
    estimate: bool = False,
    out_dir: str | Path | None = None,
    log=None,
) -> BatchResult:
    """Dub every source, isolating failures. Never overwrites a source or a good output."""
    settings = settings or Settings()
    own_runner = runner is None
    runner = runner or JobRunner(settings)
    store = JobStore(settings.workdir)
    say = log or (lambda message: None)
    result = BatchResult()

    try:
        for source in sources:
            source = str(Path(source).expanduser().resolve())
            item = BatchItem(source=source)

            # Resume: a completed job with an output on disk is not redone.
            existing = store.create(source, settings)
            if (
                not force
                and existing.status == COMPLETED
                and existing.qc_ok
                and existing.output
                and Path(existing.output).exists()
            ):
                item.job_id = existing.id
                item.status = COMPLETED
                item.qc_ok = existing.qc_ok
                item.output = existing.output
                item.skipped = True
                say(f"skip  {Path(source).name} (already dubbed)")
                result.items.append(item)
                continue

            if estimate:
                try:
                    plan = pipeline.plan(source, settings)
                    result.estimates.setdefault("files", {})[Path(source).name] = plan.estimates
                except Exception as exc:  # noqa: BLE001 - an estimate must not stop a batch
                    result.estimates.setdefault("errors", {})[Path(source).name] = str(exc)[:200]

            target_output = output_for(source, settings, out_dir)
            if (
                target_output.resolve() == Path(source).resolve()
            ):  # pragma: no cover - guarded upstream
                raise RuntimeError("refusing to write over the source file")
            say(f"dub   {Path(source).name}")
            job = runner.submit(source, settings, target_output)
            item.job_id = job.id
            final = runner.wait(job.id)
            item.status = final.status
            item.qc_ok = final.qc_ok
            item.output = final.output
            item.error = final.error
            item.duration_s = final.duration_s
            result.items.append(item)
            say(f"      {final.status}{'' if final.qc_ok is not False else ' (QC failed)'}")

            if stop_on_error and final.status in (FAILED, CANCELLED, INTERRUPTED):
                say("      stopping: --stop-on-error")
                break
    finally:
        if own_runner:
            runner.shutdown(wait=True)

    if estimate:
        totals = [
            value.get("usd")
            for value in (result.estimates.get("files") or {}).values()
            if isinstance(value, dict) and value.get("usd") is not None
        ]
        result.estimates["total_usd"] = round(sum(totals), 4) if totals else None
        result.estimates["note"] = "list-price estimates, not a quote"
    return result
