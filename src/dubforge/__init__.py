"""dubforge — dub any video into any language with several AI providers in one pipeline.

Public surface:

    from dubforge import dub, plan, Settings
    result = dub("lesson.mp4", Settings(target_lang="hi", tts="sarvam"))
    print(result.qc["ok"], result.output)

    # or as a background job an agent can poll
    from dubforge import JobRunner
    runner = JobRunner(Settings(target_lang="hi"))
    job = runner.submit("lesson.mp4")
    print(runner.wait(job.id).status)
"""

from __future__ import annotations

__version__ = "0.7.0"

from .config import Settings  # noqa: E402
from .jobs import (  # noqa: E402
    Job,
    JobError,
    JobRunner,
    JobStore,
    request_fingerprint,
)
from .pipeline import DubCancelled, DubError, DubPlan, DubResult, dub, plan  # noqa: E402

__all__ = [
    "Settings",
    "dub",
    "plan",
    "DubError",
    "DubCancelled",
    "DubPlan",
    "DubResult",
    "Job",
    "JobError",
    "JobRunner",
    "JobStore",
    "request_fingerprint",
    "__version__",
]
