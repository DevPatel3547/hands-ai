"""Pluggable LLM clients.

* :class:`ScriptedClient` -- canned replies per stage. The default for demos and tests: it
  is NOT intelligent; it lets the whole pipeline (rule gate, wiring, bench, diagnosis loop)
  run with no key, no network and no cost.
* :class:`AnthropicClient` -- Claude via the official ``anthropic`` SDK (``ANTHROPIC_API_KEY``).
* :class:`OpenAICompatClient` -- any OpenAI-compatible ``/chat/completions`` endpoint, using only
  the standard library. Works with free local models through Ollama
  (``ollama serve`` -> ``http://localhost:11434/v1``), LM Studio, llama.cpp's server, vLLM, ...

* :class:`ReplayClient` -- replays a session recorded from a real model with ``--record``. Always
  labelled REPLAY with the recorded model id and date.

Pick one with ``--model``: ``scripted`` | ``anthropic[:MODEL]`` | ``ollama[:MODEL]`` |
``openai:MODEL`` (+ ``--base-url``) | ``replay:FILE``. Whatever the model says, the deterministic gates in
:mod:`hands.rules`, :mod:`hands.fwlint` and :mod:`hands.agent` decide what reaches hardware.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("HANDS_MODEL", "claude-sonnet-5-5")
DEFAULT_OLLAMA_MODEL = os.environ.get("HANDS_OLLAMA_MODEL", "qwen2.5-coder:7b")
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/v1"
JSON_STAGES = {"design", "design_revise", "test_plan", "diagnose", "vision_wiring", "vision_state"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class LLMError(RuntimeError):
    pass


@dataclass
class LLMCall:
    stage: str
    system: str
    prompt: str
    reply: str
    images: int = 0
    seconds: float = 0.0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""


# JSON schemas for the structured stages. With Claude they are enforced through a forced tool call,
# so the reply is a parsed JSON object instead of prose that might contain JSON. The deterministic
# gates still validate everything; a schema only removes a class of parse failures.
_STR = {"type": "string"}
STAGE_SCHEMAS: dict[str, dict[str, Any]] = {
    "design": {
        "type": "object",
        "properties": {
            "name": _STR,
            "parts": {"type": "object", "additionalProperties": _STR, "description": "instance -> part type id"},
            "nets": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": _STR},
                "description": 'net name -> ["<instance>.<pin>" | "pico.GP<n>", ...]',
            },
            "pin_map": {"type": "object", "additionalProperties": _STR, "description": "role -> GP<n>"},
            "notes": _STR,
        },
        "required": ["name", "parts", "nets", "pin_map"],
    },
    "test_plan": {
        "type": "object",
        "properties": {"tests": {"type": "array", "items": {"type": "object"}}},
        "required": ["tests"],
    },
    "diagnose": {
        "type": "object",
        "properties": {"hypothesis": _STR, "action": {"type": "object"}},
        "required": ["hypothesis", "action"],
    },
}
STAGE_SCHEMAS["design_revise"] = STAGE_SCHEMAS["design"]


class LLMClient(Protocol):
    calls: list[LLMCall]

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str: ...


def _image_b64(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise LLMError(f"image {p.name} is larger than {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    if data[:3] == b"\xff\xd8\xff":
        mt = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mt = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mt = "image/webp"
    else:
        raise LLMError(f"{p.name}: only JPEG, PNG or WebP photos are supported")
    return mt, base64.standard_b64encode(data).decode()


class AnthropicClient:
    """Claude through the official SDK, streaming every request.

    * JSON stages use a forced tool call whose ``input_schema`` is :data:`STAGE_SCHEMAS` (or a free
      object for stages without one); the tool input is returned as a JSON string.
    * The system prompt is marked for prompt caching (it repeats across revisions).
    * HTTP 408/409/429/5xx/529 are retried by the SDK, which honours ``retry-after``. An
      ``overloaded_error`` that arrives *inside* the stream is retried here with backoff.
    * Every failure becomes an :class:`LLMError` with a one-line, actionable message.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 4,
        base_url: str | None = None,
        on_text: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # Check the key before the SDK import so a missing key is reported the same way whether or not
        # the optional `anthropic` package is installed (the sim and CI never install it).
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set (or use --model ollama for a free local model)")
        try:
            import anthropic  # imported lazily so the sim never needs it
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise LLMError("the Anthropic adapter needs `pip install 'hands[llm]'`") from e
        self._sdk = anthropic
        self.base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        self.timeout, self.max_retries = timeout, max_retries
        self.client = anthropic.Anthropic(api_key=key, base_url=self.base_url, timeout=timeout, max_retries=max_retries)
        self.model = model or DEFAULT_MODEL
        self.on_text = on_text
        self.sleep = sleep
        self.stream_retries = 2
        self.calls: list[LLMCall] = []

    def _request(
        self, system: str, prompt: str, stage: str, images: list[str] | None, max_tokens: int
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for path in images or []:
            mt, data = _image_b64(path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        content.append({"type": "text", "text": prompt})
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        if stage in JSON_STAGES:
            tool = f"submit_{stage}"
            req["tools"] = [
                {
                    "name": tool,
                    "description": f"Submit the {stage.replace('_', ' ')} result as structured JSON.",
                    "input_schema": STAGE_SCHEMAS.get(stage, {"type": "object"}),
                }
            ]
            req["tool_choice"] = {"type": "tool", "name": tool}
        return req

    def _error(self, e: Exception) -> LLMError:
        a = self._sdk
        where = self.base_url or "the Anthropic API"
        if isinstance(e, a.AuthenticationError):
            return LLMError("ANTHROPIC_API_KEY was rejected (HTTP 401); check the key in your shell")
        if isinstance(e, a.PermissionDeniedError):
            return LLMError(f"this API key may not use model {self.model!r} (HTTP 403)")
        if isinstance(e, a.NotFoundError):
            return LLMError(f"model {self.model!r} not found (HTTP 404); set HANDS_MODEL or --model anthropic:<id>")
        if isinstance(e, a.RateLimitError):
            return LLMError(f"rate limited by {where} (HTTP 429) after {self.max_retries} retries; wait and re-run")
        if isinstance(e, a.APITimeoutError):
            return LLMError(f"{where} did not answer within {self.timeout:g} s (after {self.max_retries} retries)")
        if isinstance(e, a.APIConnectionError):
            return LLMError(f"could not reach {where}: {e}")
        if isinstance(e, a.APIStatusError):
            code = getattr(e, "status_code", "?")
            if code == 529 or "overloaded" in str(e).lower():
                return LLMError(f"{where} is overloaded (HTTP 529) after retries; try again in a minute")
            return LLMError(f"{where} returned HTTP {code}: {_api_message(e)}")
        return LLMError(f"{type(e).__name__}: {e}")

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        req = self._request(system, prompt, stage, images, max_tokens)
        t0 = time.monotonic()
        attempt = 0
        while True:
            try:
                with self.client.messages.stream(**req) as stream:
                    if self.on_text is not None:
                        for chunk in stream.text_stream:
                            self.on_text(chunk)
                    msg = stream.get_final_message()
                break
            except self._sdk.APIStatusError as e:
                # An SSE `error` event mid-stream arrives on an HTTP 200 response and is not retried by the
                # SDK (HTTP-level 429/5xx already were, honouring retry-after). Retry those here.
                mid_stream = getattr(getattr(e, "response", None), "status_code", None) == 200
                overloaded = "overloaded" in str(e).lower() or "api_error" in str(e).lower()
                if mid_stream and overloaded and attempt < self.stream_retries:
                    attempt += 1
                    self.sleep(min(2.0**attempt, 20.0))
                    continue
                raise self._error(e) from e
            except self._sdk.APIError as e:
                raise self._error(e) from e
        stop = str(getattr(msg, "stop_reason", "") or "")
        if stage in JSON_STAGES:
            blocks = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            if not blocks:
                raise LLMError(f"{stage}: the model did not return the structured result (stop_reason={stop})")
            text = json.dumps(getattr(blocks[0], "input", {}))
        else:
            text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        if stop == "max_tokens":
            raise LLMError(f"{stage}: reply was cut off at max_tokens={max_tokens}")
        if stop == "refusal":
            raise LLMError(f"{stage}: the model declined to answer")
        u = getattr(msg, "usage", None)
        self.calls.append(
            LLMCall(
                stage,
                system,
                prompt,
                text,
                len(images or []),
                time.monotonic() - t0,
                model=str(getattr(msg, "model", self.model)),
                input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                stop_reason=stop,
            )
        )
        return text


def _api_message(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(e)[:300]


Opener = Callable[[urllib.request.Request, float], bytes]


def _default_open(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - scheme validated in __init__
        data: bytes = r.read()
        return data


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat client (stdlib only), with timeouts and retries."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 300.0,
        retries: int = 3,
        json_mode: bool = True,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise LLMError(f"base URL must start with http:// or https:// (got {base_url!r})")
        self.model, self.base_url = model, base_url.rstrip("/")
        self.api_key = api_key
        self.timeout, self.retries, self.json_mode = timeout, max(1, retries), json_mode
        self.open = opener or _default_open
        self.sleep = sleep
        self.calls: list[LLMCall] = []

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        user: str | list[dict[str, Any]] = prompt
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for path in images:
                mt, data = _image_b64(path)
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}})
            user = parts
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode and stage in JSON_STAGES:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(  # noqa: S310 - http(s) only, checked in __init__
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        t0 = time.monotonic()
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                raw = self.open(req, self.timeout)
                payload = json.loads(raw)
                text = payload["choices"][0]["message"]["content"] or ""
                self.calls.append(LLMCall(stage, system, prompt, text, len(images or []), time.monotonic() - t0))
                return str(text)
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (408, 409, 429) and e.code < 500:
                    raise LLMError(f"{self.base_url}: HTTP {e.code} {e.reason}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"{self.base_url}: unexpected response: {e}") from e
            if attempt + 1 < self.retries:
                self.sleep(min(2.0**attempt, 10.0))
        raise LLMError(
            f"{self.base_url} unreachable after {self.retries} attempts: {last}. "
            f"Is the server running (e.g. `ollama serve`)?"
        )


@dataclass
class ScriptedClient:
    """Replies come from ``script[stage]`` in order (the last one repeats).
    A value may be a callable ``f(prompt) -> str`` for prompt-dependent stubs."""

    script: dict[str, list[str | Callable[[str], str]]]
    calls: list[LLMCall] = field(default_factory=list)
    _pos: dict[str, int] = field(default_factory=dict)

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        if stage not in self.script:
            raise KeyError(f"ScriptedClient has no reply for stage {stage!r}")
        replies = self.script[stage]
        i = self._pos.get(stage, 0)
        r = replies[min(i, len(replies) - 1)]
        self._pos[stage] = i + 1
        text = r(prompt) if callable(r) else r
        self.calls.append(LLMCall(stage, system, prompt, text, len(images or [])))
        return text


class ReplayError(LLMError):
    pass


REPLAY_FORMAT = "hands-replay/1"


class RecordingClient:
    """Wraps a real client and appends every call to a JSONL replay file (header line first)."""

    def __init__(self, inner: LLMClient, path: str | Path, model: str):
        self.inner, self.path, self.model = inner, Path(path), model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "format": REPLAY_FORMAT,
            "model": model,
            "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": "Recorded from a real model by `hands ... --record`. Replays print REPLAY.",
        }
        self.path.write_text(json.dumps(header) + "\n", encoding="utf-8")

        self.calls: list[LLMCall] = inner.calls  # the same list object: calls are counted once

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        reply = self.inner.complete(system, prompt, stage=stage, images=images, max_tokens=max_tokens)
        c = self.inner.calls[-1] if self.inner.calls else None
        rec = {
            "stage": stage,
            "prompt_sha": _sha(system + "\0" + prompt),
            "reply": reply,
            "images": len(images or []),
            "model": c.model if c else self.model,
            "usage": {"input": c.input_tokens, "output": c.output_tokens} if c else {},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return reply


class ReplayClient:
    """Serves the replies of a recorded session, per stage, in order. Never calls a model."""

    def __init__(self, path: str | Path):
        p = Path(path)
        if not p.is_file():
            raise ReplayError(f"replay file {str(p)!r} not found (record one with `make demo-live`)")
        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if not lines or lines[0].get("format") != REPLAY_FORMAT:
            raise ReplayError(f"{p.name} is not a {REPLAY_FORMAT} replay file")
        self.header: dict[str, Any] = lines[0]
        self.model = str(self.header.get("model", "?"))
        self.recorded = str(self.header.get("recorded", "?"))
        self._by_stage: dict[str, list[str]] = {}
        for rec in lines[1:]:
            self._by_stage.setdefault(str(rec["stage"]), []).append(str(rec["reply"]))
        self._pos: dict[str, int] = {}
        self.calls: list[LLMCall] = []

    @property
    def label(self) -> str:
        return f"REPLAY of {self.model} recorded {self.recorded}"

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        replies = self._by_stage.get(stage)
        i = self._pos.get(stage, 0)
        if not replies or i >= len(replies):
            raise ReplayError(
                f"the replay has no more '{stage}' replies (the run diverged from the recording; "
                "replays are only exact for the same gadget, faults and Hands version)"
            )
        self._pos[stage] = i + 1
        self.calls.append(LLMCall(stage, system, prompt, replies[i], len(images or []), model=f"replay:{self.model}"))
        return replies[i]


def _sha(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:16]


PROVIDERS = ("scripted", "anthropic", "ollama", "openai", "replay")


def parse_model_spec(spec: str) -> tuple[str, str | None]:
    """``"ollama:llama3.1:8b"`` -> ``("ollama", "llama3.1:8b")``; ``"scripted"`` -> ``("scripted", None)``."""
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise LLMError(
            f"unknown model provider {provider!r}; use scripted, anthropic[:MODEL], ollama[:MODEL], "
            "openai:MODEL or replay:FILE"
        )
    return provider, (model.strip() or None)


def make_client(
    spec: str = "scripted",
    *,
    scripted: ScriptedClient | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
    record: str | Path | None = None,
) -> LLMClient:
    """Build a client from a ``--model`` spec, optionally recording every reply to ``record``."""
    provider, model = parse_model_spec(spec)
    client: LLMClient
    if provider == "scripted":
        if scripted is None:
            raise LLMError("the scripted model only knows the built-in gadgets; pick a real model with --model")
        client = scripted
    elif provider == "replay":
        if not model:
            raise LLMError("replay needs a file: --model replay:replays/steelers-claude.jsonl")
        client = ReplayClient(model)
    elif provider == "anthropic":
        client = AnthropicClient(model, api_key, timeout=timeout or 120.0, base_url=base_url)
    elif provider == "ollama":
        client = OpenAICompatClient(
            model or DEFAULT_OLLAMA_MODEL, base_url or DEFAULT_OLLAMA_URL, api_key, timeout=timeout or 600.0
        )
    else:
        url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not model:
            raise LLMError("openai provider needs a model name, e.g. --model openai:gpt-4o-mini")
        client = OpenAICompatClient(model, url, api_key or os.environ.get("OPENAI_API_KEY"), timeout=timeout or 300.0)
    if record:
        if provider in ("scripted", "replay"):
            raise LLMError("--record only makes sense with a real model (anthropic, ollama or openai)")
        client = RecordingClient(client, record, getattr(client, "model", model or provider))
    return client
