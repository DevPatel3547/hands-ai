"""Pluggable LLM clients.

* :class:`ScriptedClient` -- canned replies per stage. The default for demos and tests: it
  is NOT intelligent; it lets the whole pipeline (rule gate, wiring, bench, diagnosis loop)
  run with no key, no network and no cost.
* :class:`AnthropicClient` -- Claude via the official ``anthropic`` SDK (``ANTHROPIC_API_KEY``).
* :class:`OpenAICompatClient` -- any OpenAI-compatible ``/chat/completions`` endpoint, using only
  the standard library. Works with free local models through Ollama
  (``ollama serve`` -> ``http://localhost:11434/v1``), LM Studio, llama.cpp's server, vLLM, ...

Pick one with ``--model``: ``scripted`` | ``anthropic[:MODEL]`` | ``ollama[:MODEL]`` |
``openai:MODEL`` (+ ``--base-url``). Whatever the model says, the deterministic gates in
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
    def __init__(
        self, model: str | None = None, api_key: str | None = None, timeout: float = 120.0, max_retries: int = 3
    ):
        try:
            import anthropic  # imported lazily so the sim never needs it
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise LLMError("the Anthropic adapter needs `pip install 'hands[llm]'`") from e
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set (or use --model ollama for a free local model)")
        self.client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)
        self.model = model or DEFAULT_MODEL
        self.calls: list[LLMCall] = []

    def complete(
        self, system: str, prompt: str, *, stage: str, images: list[str] | None = None, max_tokens: int = 4096
    ) -> str:
        content: list[dict[str, Any]] = []
        for path in images or []:
            mt, data = _image_b64(path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        content.append({"type": "text", "text": prompt})
        t0 = time.monotonic()
        msg = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}]
        )
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        self.calls.append(LLMCall(stage, system, prompt, text, len(images or []), time.monotonic() - t0))
        return text


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


def parse_model_spec(spec: str) -> tuple[str, str | None]:
    """``"ollama:llama3.1:8b"`` -> ``("ollama", "llama3.1:8b")``; ``"scripted"`` -> ``("scripted", None)``."""
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    if provider not in ("scripted", "anthropic", "ollama", "openai"):
        raise LLMError(
            f"unknown model provider {provider!r}; use scripted, anthropic[:MODEL], ollama[:MODEL] or openai:MODEL"
        )
    return provider, (model.strip() or None)


def make_client(
    spec: str = "scripted",
    *,
    scripted: ScriptedClient | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> LLMClient:
    """Build a client from a ``--model`` spec."""
    provider, model = parse_model_spec(spec)
    if provider == "scripted":
        if scripted is None:
            raise LLMError("the scripted model only knows the built-in gadgets; pick a real model with --model")
        return scripted
    if provider == "anthropic":
        return AnthropicClient(model, api_key, timeout=timeout or 120.0)
    if provider == "ollama":
        return OpenAICompatClient(
            model or DEFAULT_OLLAMA_MODEL, base_url or DEFAULT_OLLAMA_URL, api_key, timeout=timeout or 600.0
        )
    url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not model:
        raise LLMError("openai provider needs a model name, e.g. --model openai:gpt-4o-mini")
    return OpenAICompatClient(model, url, api_key or os.environ.get("OPENAI_API_KEY"), timeout=timeout or 300.0)
