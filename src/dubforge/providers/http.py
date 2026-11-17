"""HTTP plumbing shared by every network adapter.

Three rules, all learned the hard way in dubbing pipelines:

* every call has a timeout and a bounded retry, because a hung TTS request in the
  middle of a 900-segment film is indistinguishable from a hang;
* a failure is *classified*, not just reported — `rate_limit` is worth retrying later,
  `auth` is not worth retrying at all, and a caller needs to tell them apart;
* an error message may contain the status code and a short slice of the response, and
  must never contain a request header.
"""

from __future__ import annotations

import contextvars
import time
from typing import Any

from .base import ProviderError

# A per-thread list the caller can install to count retries, so a job can record "this
# run retried twice" without every adapter growing a return value for it.
_RETRY_SINK: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "dubforge_retry_sink", default=None
)


def watch_retries(sink: list) -> None:
    """Install a list that accumulates retry descriptions for the current context."""
    _RETRY_SINK.set(sink)


def _note_retry(provider: str, reason: str, attempt: int) -> None:
    sink = _RETRY_SINK.get()
    if sink is not None:
        sink.append({"provider": provider, "reason": reason, "attempt": attempt + 1})


# Status → (kind, retryable). One table, so every adapter classifies failures the same way.
_STATUS_KINDS: dict[int, tuple[str, bool]] = {
    400: ("bad_request", False),
    401: ("auth", False),
    403: ("auth", False),
    404: ("not_found", False),
    409: ("conflict", False),
    413: ("too_large", False),
    422: ("bad_request", False),
    429: ("rate_limit", True),
    500: ("server", True),
    502: ("server", True),
    503: ("server", True),
    504: ("timeout", True),
}


def classify(status: int) -> tuple[str, bool]:
    """(kind, retryable) for an HTTP status code."""
    if status in _STATUS_KINDS:
        return _STATUS_KINDS[status]
    if 500 <= status < 600:
        return "server", True
    return "unknown", False


def _retry_after(resp, default: float) -> float:
    """Honour a vendor's Retry-After instead of guessing a backoff."""
    try:
        value = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    except AttributeError:
        return default
    if not value:
        return default
    try:
        return max(0.0, min(float(value), 30.0))
    except (TypeError, ValueError):
        return default


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
    idempotent: bool = False,
    idempotency_key: str | None = None,
):
    """POST, retrying only when a retry cannot cost the user money twice.

    Two kinds of failure, and they are not the same:

    * the vendor **answered** with 429 or 5xx — the request was rejected, so retrying is
      free and correct;
    * the connection **timed out** or dropped — the vendor may have accepted and billed
      the request. Retrying then is how one dub becomes two charges.

    So a network failure is retried only when the call is idempotent (`idempotent=True`)
    or carries a vendor-documented idempotency key. Otherwise it raises
    `ProviderError(kind="ambiguous")` and says plainly that the outcome is unknown. No
    idempotency header is invented: `idempotency_key` is only passed by adapters whose
    vendor documents it (OpenAI, as `Idempotency-Key`).
    """
    requests = _requests()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            call_headers = dict(headers or {})
            if idempotency_key:
                call_headers["Idempotency-Key"] = idempotency_key
            resp = requests.post(
                url,
                headers=call_headers,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
            )
            if resp.status_code in ok_status:
                resp.dubforge_attempts = attempt + 1
                return resp
            kind, retryable = classify(resp.status_code)
            if retryable and attempt < retries:
                _note_retry(provider, f"HTTP {resp.status_code} ({kind})", attempt)
                time.sleep(_retry_after(resp, min(2**attempt, 8)))
                continue
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ProviderError(
                f"{provider} returned HTTP {resp.status_code} ({kind}): {snippet}",
                kind=kind,
                status=resp.status_code,
                retryable=retryable,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - the network stack raises many types
            last = exc
            safe_to_retry = idempotent or bool(idempotency_key)
            if safe_to_retry and attempt < retries:
                _note_retry(provider, f"{type(exc).__name__} (retrying: idempotent)", attempt)
                time.sleep(min(2**attempt, 8))
                continue
            if not safe_to_retry:
                raise ProviderError(
                    f"{provider} did not answer ({type(exc).__name__}: {exc}). "
                    "The request may have been accepted and billed, so it was not retried. "
                    "Check the provider dashboard before re-running this line.",
                    kind="ambiguous",
                    retryable=False,
                ) from exc
            raise ProviderError(
                f"{provider} request failed: {type(exc).__name__}: {exc}",
                kind="network",
                retryable=True,
            ) from exc
    raise ProviderError(f"{provider} request failed: {last}", kind="network", retryable=True)


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
            kind, retryable = classify(resp.status_code)
            if retryable and attempt < retries:
                _note_retry(provider, f"HTTP {resp.status_code} ({kind})", attempt)
                time.sleep(_retry_after(resp, min(2**attempt, 8)))
                continue
            raise ProviderError(
                f"{provider} returned HTTP {resp.status_code} ({kind}): {(resp.text or '')[:300]}",
                kind=kind,
                status=resp.status_code,
                retryable=retryable,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
                continue
            raise ProviderError(
                f"{provider} request failed: {type(exc).__name__}: {exc}",
                kind="network",
                retryable=True,
            ) from exc
    raise ProviderError(f"{provider} request failed", kind="network", retryable=True)


def download(
    url: str, dst: str, *, headers: dict[str, str] | None = None, provider: str = "provider"
) -> str:
    resp = get(url, headers=headers, provider=provider, timeout=600)
    with open(dst, "wb") as handle:
        handle.write(resp.content)
    return dst


def probe_json(url: str, headers: dict[str, str] | None = None, provider: str = "provider") -> str:
    """A cheap authenticated GET that proves the credential works, without reading data.

    Used by `Provider.probe()` so `dubforge_check_provider_health` can distinguish
    "key is set" from "key actually works" for the adapters that have a free endpoint.
    """
    resp = get(url, headers=headers, provider=provider, timeout=20, retries=0)
    return f"HTTP {resp.status_code} from {url.split('?')[0]}"
