"""Lip sync: optional, and honest about what it needs.

Audio dubbing never touches the picture. Lip sync re-renders the mouth to match the
new language, which is a different product with a different cost and, for real
people on camera, a consent question. It is off by default and the adapter says
plainly that it needs a publicly reachable URL, because that is what the vendor
fetches — the pipeline does not upload your footage to a bucket for you.
"""

from __future__ import annotations

import time

from .base import LipSync, ProviderError, Spec
from .http import get, post


class SyncLabsLipSync(LipSync):
    spec = Spec(
        name="synclabs",
        kind="lipsync",
        title="Sync.so (Sync-2)",
        env=("SYNCLABS_API_KEY",),
        capabilities=("mouth_reshaping", "async_job"),
        languages="n/a — language agnostic",
        verified=False,
        quality="Convincing on talking heads; falls apart on fast cuts, occlusions and hands near the face.",
        notes="Needs HTTP(S) URLs for both inputs (a signed link to the dubbed mp4 and the audio). "
        "Quote-based pricing. Off by default: --lipsync synclabs --public-base https://…",
        docs="https://docs.sync.so/api-reference",
    )

    def sync(self, video: str, audio: str, out_path: str) -> str:
        self.require()
        for label, value in (("video", video), ("audio", audio)):
            if not str(value).startswith(("http://", "https://")):
                raise ProviderError(
                    f"lip sync needs a public URL for the {label}, got a local path. "
                    "Upload the dubbed file somewhere the vendor can fetch, pass that URL, "
                    "then keep the returned file. The pipeline deliberately does not upload for you."
                )
        key = self.settings.provider_key("synclabs") or ""
        resp = post(
            "https://api.sync.so/v2/generate",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json_body={
                "model": "sync-2",
                "input": [{"type": "video", "url": video}, {"type": "audio", "url": audio}],
                "options": {"sync_mode": "cut_off"},
            },
            timeout=self.settings.timeout_s,
            provider="synclabs",
        )
        job = resp.json()
        job_id = job.get("id")
        if not job_id:
            raise ProviderError(f"synclabs returned no job id: {str(job)[:200]}")
        deadline = time.time() + 3600
        while time.time() < deadline:
            status = get(
                f"https://api.sync.so/v2/generate/{job_id}",
                headers={"x-api-key": key},
                provider="synclabs",
            ).json()
            state = str(status.get("status", "")).lower()
            if state in ("completed", "complete", "succeeded"):
                url = status.get("outputUrl") or status.get("output_url")
                if not url:
                    raise ProviderError("synclabs completed without an output URL")
                from .http import download

                return download(url, out_path, provider="synclabs")
            if state in ("failed", "error"):
                raise ProviderError(f"synclabs job failed: {str(status)[:200]}")
            time.sleep(10)
        raise ProviderError("synclabs job did not finish within an hour")


class NoLipSync(LipSync):
    spec = Spec(
        name="none",
        kind="lipsync",
        title="No lip sync (audio only)",
        verified=True,
        quality="The picture is untouched — the normal, cheap, honest default.",
    )

    def sync(self, video: str, audio: str, out_path: str) -> str:
        return video
