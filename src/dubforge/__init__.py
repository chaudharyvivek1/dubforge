"""dubforge — dub any video into any language with several AI providers in one pipeline.

Public surface:

    from dubforge import dub, plan, Settings
    result = dub("lesson.mp4", Settings(target_lang="hi", tts="sarvam"))
    print(result.qc["ok"], result.output)
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Settings  # noqa: E402
from .pipeline import DubError, DubPlan, DubResult, dub, plan  # noqa: E402

__all__ = ["Settings", "dub", "plan", "DubError", "DubPlan", "DubResult", "__version__"]
