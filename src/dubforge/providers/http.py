"""HTTP plumbing shared by every network adapter.

Two rules, both learned the hard way in dubbing pipelines:

* every call has a timeout and a bounded retry, because a hung TTS request in the
  middle of a 900-segment film is indistinguishable from a hang;
* an error message may contain the status code and a short slice of the response,
  and must never contain a request header.
"""

from __future__ import annotations

import time
from typing import Any

from .base import ProviderError


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise ProviderError("the 'requests' package is missing: pip install requests") from exc
    return requests


def post(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: int = 600,
    retries: int = 2,
    provider: str = "provider",
    ok_status: tuple[int, ...] = (200, 201, 202),
):
    """POST with retries on 429/5xx/connection errors. Returns the response object."""
    requests = _requests()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url,
                headers=headers or {},
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
            )
            if resp.status_code in ok_status:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ProviderError(f"{provider} returned HTTP {resp.status_code}: {snippet}")
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - network stack raises many types
            last = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            raise ProviderError(f"{provider} request failed: {type(exc).__name__}: {exc}") from exc
    raise ProviderError(f"{provider} request failed: {last}")


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    timeout: int = 120,
    retries: int = 2,
    provider: str = "provider",
) -> Any:
    requests = _requests()
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers or {}, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            raise ProviderError(
                f"{provider} returned HTTP {resp.status_code}: {(resp.text or '')[:300]}"
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            raise ProviderError(f"{provider} request failed: {type(exc).__name__}: {exc}") from exc
    raise ProviderError(f"{provider} request failed")


def download(
    url: str, dst: str, *, headers: dict[str, str] | None = None, provider: str = "provider"
) -> str:
    resp = get(url, headers=headers, provider=provider, timeout=600)
    with open(dst, "wb") as handle:
        handle.write(resp.content)
    return dst
