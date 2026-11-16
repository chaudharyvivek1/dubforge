"""Translation adapters.

The prompt is the product here. A dubbing translation is not a document
translation: it has to be sayable in the time the mouth was moving, keep the
speaker's register, and leave names and product terms alone. Every adapter sends
the same brief, so switching vendors changes the model, not the craft rules.

The rewrite loop lives in the pipeline: any line whose estimate exceeds its window
is sent back with `instructions[index]`, which becomes an explicit character
budget in the prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from ..media import Segment
from .base import ProviderError, Spec, Translator
from .http import post

SYSTEM = (
    "You are a professional audiovisual translator and dubbing script adaptor. "
    "You translate dialogue that will be spoken aloud by a voice actor and mixed "
    "under the original music. You are judged on two things at once: whether the "
    "line means what the original meant, and whether it can be said in the time "
    "available without sounding rushed."
)

RULES = """
Rules, in order of importance:
1. Say what the line means. Never add information, never drop a fact the line exists to carry, never explain.
2. Fit the time. Each line has a target duration; aim for natural speech at that length. If a line cannot fit, keep the meaning and cut filler, repetition, honorifics and redundant pronouns — never cut the point.
3. Stay in character. Match register, age, formality and attitude. A threat stays a threat; a joke stays funny in the target culture.
4. Speak, don't write. Short clauses, natural word order, no literary connectors, no bracketed notes, no ellipses for suspense.
5. Names, brands, numbers and units: keep the referent. Write numbers the way a speaker says them in the target language.
6. Leave placeholders and bracketed tags such as [laughs] or {name} exactly as they are.
7. Output only the requested JSON. No commentary, no romanisation, no source text.
"""


def _glossary_block(glossary: dict[str, str] | None) -> str:
    if not glossary:
        return ""
    lines = [f'- "{k}" -> "{v}"' for k, v in glossary.items()]
    return "\nTerminology (use exactly these renderings):\n" + "\n".join(lines) + "\n"


def build_prompt(
    segments: list[Segment],
    source: str,
    target: str,
    glossary: dict[str, str] | None = None,
    instructions: dict[int, str] | None = None,
    context: str = "",
) -> str:
    rows = []
    for seg in segments:
        row = {
            "i": seg.index,
            "seconds": round(seg.window or seg.duration, 2),
            "speaker": seg.speaker,
            "text": seg.text,
        }
        if instructions and seg.index in instructions:
            row["constraint"] = instructions[seg.index]
        rows.append(row)
    payload = json.dumps(rows, ensure_ascii=False, indent=1)
    head = f"Translate from {source} into {target}."
    if context:
        head += f"\nContext for terminology and tone: {context}"
    tail = (
        f"\n\nReturn a JSON array with one object per input line, in the same order, "
        f'each of the form {{"i": <the i value>, "t": "<the line in {target}>"}}. '
        f"Nothing else."
    )
    return f"{head}\n{RULES}{_glossary_block(glossary)}\nLines:\n{payload}{tail}"


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_response(text: str, segments: list[Segment]) -> dict[int, str]:
    """Pull {index: translation} out of a model response, tolerating fences and preamble."""
    if not text or not text.strip():
        raise ProviderError("translator returned an empty response")
    body = text
    fenced = _FENCE.search(body)
    if fenced:
        body = fenced.group(1)
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end <= start:
        raise ProviderError(f"translator did not return a JSON array: {body[:200]!r}")
    try:
        rows = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"translator returned malformed JSON: {exc}") from exc
    wanted = {s.index for s in segments}
    out: dict[int, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        text_value = row.get("t") or row.get("text") or row.get("translation")
        if idx in wanted and isinstance(text_value, str) and text_value.strip():
            out[idx] = text_value.strip()
    if not out:
        raise ProviderError("translator response had no usable lines")
    return out


class LLMTranslator(Translator):
    """Batch an LLM, apply the result, mark every segment it touched."""

    batch_size = 40

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def translate(
        self,
        segments: list[Segment],
        source: str,
        target: str,
        glossary: dict[str, str] | None = None,
        instructions: dict[int, str] | None = None,
        context: str = "",
    ) -> None:
        self.require()
        items = list(segments)
        if not items:
            return
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            prompt = build_prompt(batch, source, target, glossary, instructions, context)
            raw = self.complete(SYSTEM, prompt)
            mapping = parse_response(raw, batch)
            for seg in batch:
                if seg.index in mapping:
                    if seg.target_text is not None and seg.target_text != mapping[seg.index]:
                        seg.rewrites += 1
                    seg.target_text = mapping[seg.index]
                    seg.translator = self.spec.name
                elif seg.target_text is None:
                    seg.notes.append("translator skipped this line; source text kept")
                    seg.target_text = seg.text
                    seg.translator = f"{self.spec.name}-partial"


class OpenAITranslator(LLMTranslator):
    spec = Spec(
        name="openai",
        kind="translate",
        title="OpenAI chat models",
        env=("OPENAI_API_KEY",),
        capabilities=("duration_aware", "glossary", "context"),
        languages="100+",
        verified=False,
        quality="Reliable instruction-following on length constraints; the default for most languages.",
        notes="Set DUBFORGE_TRANSLATE_MODEL to pin a model; the default moves as models are retired.",
        docs="https://platform.openai.com/docs/api-reference/chat",
    )

    def complete(self, system: str, user: str) -> str:
        key = self.settings.provider_key("openai") or ""
        resp = post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json_body={
                "model": self.settings.translate_model or "gpt-4.1-mini",
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=self.settings.timeout_s,
            provider="openai",
        )
        payload = resp.json()
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"openai returned an unexpected shape: {str(payload)[:200]}"
            ) from exc


class GeminiTranslator(LLMTranslator):
    spec = Spec(
        name="gemini",
        kind="translate",
        title="Google Gemini",
        env=("GEMINI_API_KEY",),
        capabilities=("duration_aware", "glossary", "context", "long_context"),
        languages="100+",
        verified=False,
        quality="Good on Indian languages and long context; cheap per token.",
        notes="Set DUBFORGE_TRANSLATE_MODEL (default gemini-2.5-flash). GEMINI_API_KEY or GOOGLE_API_KEY.",
        docs="https://ai.google.dev/gemini-api/docs/text-generation",
    )

    def missing(self) -> list[str]:
        import os

        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return []
        return ["environment variable GEMINI_API_KEY (or GOOGLE_API_KEY) is not set"]

    def complete(self, system: str, user: str) -> str:
        key = self.settings.key("GEMINI_API_KEY", "GOOGLE_API_KEY") or ""
        model = self.settings.translate_model or "gemini-2.5-flash"
        resp = post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json_body={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.3},
            },
            timeout=self.settings.timeout_s,
            provider="gemini",
        )
        payload = resp.json()
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"gemini returned an unexpected shape: {str(payload)[:200]}"
            ) from exc


class SarvamTranslator(Translator):
    spec = Spec(
        name="sarvam",
        kind="translate",
        title="Sarvam Mayura (Indic languages)",
        env=("SARVAM_API_KEY",),
        capabilities=("indic", "glossary", "transliteration"),
        languages="11 Indian languages + English",
        verified=False,
        quality="Purpose-built for Indic translation; usually beats general models on Hindi, Tamil, Telugu, Bengali idiom.",
        notes="Translates one line at a time and has no duration constraint — pair it with --translator openai "
        "for the rewrite pass, or accept the stretch budget.",
        docs="https://docs.sarvam.ai/api-reference-docs/text/translate",
    )

    def translate(
        self, segments, source, target, glossary=None, instructions=None, context=""
    ) -> None:
        self.require()
        key = self.settings.provider_key("sarvam") or ""
        source_code = "auto" if source in ("auto", "", None) else source
        for seg in segments:
            resp = post(
                "https://api.sarvam.ai/translate",
                headers={"api-subscription-key": key, "Content-Type": "application/json"},
                json_body={
                    "input": seg.text,
                    "source_language_code": source_code,
                    "target_language_code": target,
                    "model": self.settings.translate_model or "mayura:v1",
                    "enable_preprocessing": True,
                },
                timeout=self.settings.timeout_s,
                provider="sarvam",
            )
            payload = resp.json()
            out = payload.get("translated_text") or payload.get("output")
            if not out:
                raise ProviderError(f"sarvam returned no translation: {str(payload)[:200]}")
            seg.target_text = apply_glossary(str(out).strip(), glossary)
            seg.translator = "sarvam"
            if instructions and seg.index in instructions:
                seg.notes.append("sarvam cannot shorten on request; timing handled by atempo")


def deepl_endpoint(api_key: str) -> str:
    """Free keys end in `:fx` and only work on the free host; paid keys only on the pro host.

    Using the wrong one returns 403, which reads like a bad key and wastes an hour.
    """
    return (
        "https://api-free.deepl.com/v2/translate"
        if api_key.strip().endswith(":fx")
        else "https://api.deepl.com/v2/translate"
    )


def apply_glossary(text: str, glossary: dict[str, str] | None) -> str:
    """Pin terminology in the *output*.

    A provider without prompt control cannot be told about a glossary, so the pinned
    rendering is enforced afterwards: any source term that survived the translation is
    replaced. Replacing terms in the *input* instead — which this used to do — corrupts
    the sentence the translator is asked to translate.
    """
    if not glossary:
        return text
    out = text
    for term, rendering in glossary.items():
        if term and term in out:
            out = out.replace(term, rendering)
    return out


class DeepLTranslator(Translator):
    spec = Spec(
        name="deepl",
        kind="translate",
        title="DeepL",
        env=("DEEPL_API_KEY",),
        capabilities=("fast", "glossary"),
        languages="30+ European and Asian languages",
        verified=False,
        quality="Excellent fluency in its supported pairs; no duration control and no Indic languages beyond a few.",
        notes="Cheapest per character for European pairs. Ignores the length constraint: over-long lines are "
        "handled by atempo only.",
        docs="https://developers.deepl.com/docs",
    )

    def translate(
        self, segments, source, target, glossary=None, instructions=None, context=""
    ) -> None:
        self.require()
        key = self.settings.provider_key("deepl") or ""
        body: dict = {"text": [s.text for s in segments], "target_lang": target.upper()}
        if source and source != "auto":
            body["source_lang"] = source.upper()
        resp = post(
            deepl_endpoint(key),
            headers={"Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json"},
            json_body=body,
            timeout=self.settings.timeout_s,
            provider="deepl",
        )
        rows = resp.json().get("translations") or []
        if len(rows) != len(segments):
            raise ProviderError("deepl returned a different number of lines than it was given")
        for seg, row in zip(segments, rows, strict=False):
            seg.target_text = apply_glossary(str(row.get("text", "")).strip(), glossary)
            seg.translator = "deepl"
            if instructions and seg.index in instructions:
                seg.notes.append("deepl cannot shorten on request; timing handled by atempo")


class OllamaTranslator(LLMTranslator):
    spec = Spec(
        name="ollama",
        kind="translate",
        title="Local model via Ollama (free, private)",
        env=(),
        capabilities=("offline", "duration_aware", "glossary", "private"),
        languages="whatever the local model handles",
        verified=False,
        quality="Depends entirely on the model you pull; a 7B model is serviceable for European pairs, "
        "weak on idiom. Nothing leaves the machine, which matters for student footage.",
        notes="Needs `ollama serve` on DUBFORGE_OLLAMA_URL (default http://localhost:11434) and a model: "
        "`ollama pull qwen2.5:14b-instruct`. Set DUBFORGE_TRANSLATE_MODEL to that model name.",
        docs="https://github.com/ollama/ollama/blob/main/docs/api.md",
    )

    def missing(self) -> list[str]:
        """A local server is the dependency here, not a key — so actually check it."""
        import os

        gaps = super().missing()
        base = os.environ.get("DUBFORGE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
        try:
            import requests

            requests.get(f"{base}/api/tags", timeout=1.5).raise_for_status()
        except Exception:  # noqa: BLE001 - any failure means "no server there"
            gaps.append(f"no Ollama server answering at {base} (start `ollama serve`)")
        return gaps

    def complete(self, system: str, user: str) -> str:
        import os

        base = os.environ.get("DUBFORGE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
        resp = post(
            f"{base}/api/chat",
            json_body={
                "model": self.settings.translate_model or "qwen2.5:14b-instruct",
                "stream": False,
                "options": {"temperature": 0.3},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=self.settings.timeout_s,
            provider="ollama",
        )
        payload = resp.json()
        return str((payload.get("message") or {}).get("content", ""))


def context_from_settings(settings) -> str:
    bits: Iterable[str] = [b for b in (settings.style, settings.emotion) if b]
    return ". ".join(bits)
