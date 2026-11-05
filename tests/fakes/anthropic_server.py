"""A local fake of the Anthropic Messages API, faithful to the wire format.

It speaks ``POST /v1/messages`` with JSON or Server-Sent-Events streaming (``message_start``,
``content_block_start``, ``ping``, ``content_block_delta`` with ``text_delta`` and
``input_json_delta``, ``content_block_stop``, ``message_delta``, ``message_stop``, and
mid-stream ``error`` events), validates the headers and body the real API requires, and can
inject 429 / 529 responses with ``retry-after``, slow responses (timeouts) and error events.

The real ``anthropic`` SDK is pointed at it with ``base_url``, so the live code path in
:class:`hands.llm.AnthropicClient` runs unmodified. Used by the tests and by
``make demo-live-fake``:

    python -m tests.fakes.anthropic_server --gadget steelers --port 8790
"""

from __future__ import annotations

import argparse
import itertools
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[dict[str, Any]], dict[str, Any]]
_ids = itertools.count(1)


def default_responder(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("tools"):
        return {"tool_input": {}}
    return {"text": "ok"}


def gadget_responder(gadget_id: str) -> Responder:
    """Answer like the scripted model for a built-in gadget, but through the real wire format."""
    from hands.gadgets import registry

    scripted = registry()[gadget_id].scripted_client()

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        tools = body.get("tools") or []
        stage = tools[0]["name"].removeprefix("submit_") if tools else "firmware"
        system = "".join(b.get("text", "") for b in body.get("system", []) if isinstance(b, dict))
        user = body["messages"][-1]["content"]
        prompt = "".join(p.get("text", "") for p in user if isinstance(p, dict)) if isinstance(user, list) else user
        reply = scripted.complete(system, prompt, stage=stage)
        if tools:
            return {"tool_input": json.loads(reply)}
        return {"text": reply}

    return respond


class FakeAnthropic:
    """Start with :meth:`start`; ``faults`` is a queue consumed one per request before normal replies.

    Fault dicts: ``{"status": 429|529|500|401|404|400, "headers": {...}, "error_type": str}``,
    ``{"delay": seconds}`` (then answer normally), ``{"stream_error": "overloaded_error"}``
    (send ``message_start`` then an SSE ``error`` event).
    """

    def __init__(self, responder: Responder | None = None, api_key: str = "test-key"):
        self.responder = responder or default_responder
        self.api_key = api_key
        self.faults: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.times: list[float] = []
        self._srv: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self, port: int = 0) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                fake._handle(self)

            def do_GET(self) -> None:
                fake._models(self)

        self._srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    # ------------------------------------------------------------------ protocol
    @staticmethod
    def _json(h: BaseHTTPRequestHandler, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("request-id", f"req_fake_{next(_ids)}")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _error(self, h: BaseHTTPRequestHandler, code: int, etype: str, msg: str, headers: dict | None = None) -> None:
        self._json(h, code, {"type": "error", "error": {"type": etype, "message": msg}}, headers)

    def _validate(self, h: BaseHTTPRequestHandler, body: Any) -> str | None:
        if h.path.split("?")[0] != "/v1/messages":
            return f"unknown path {h.path}"
        if not h.headers.get("anthropic-version"):
            return "anthropic-version header is required"
        if not isinstance(body, dict):
            return "body must be a JSON object"
        if not isinstance(body.get("model"), str) or not body["model"]:
            return "model: field required"
        if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
            return "max_tokens: field required"
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or msgs[0].get("role") != "user":
            return "messages: first message must use the user role"
        sysp = body.get("system")
        if sysp is not None and not isinstance(sysp, (str, list)):
            return "system: must be a string or a list of text blocks"
        tc = body.get("tool_choice")
        if tc and tc.get("type") == "tool":
            names = {t.get("name") for t in body.get("tools") or []}
            if tc.get("name") not in names:
                return f"tool_choice names unknown tool {tc.get('name')!r}"
        for t in body.get("tools") or []:
            if not isinstance(t.get("input_schema"), dict) or t["input_schema"].get("type") != "object":
                return f"tools.{t.get('name')}.input_schema: must be a JSON schema of type object"
        return None

    models = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")

    def _authorized(self, h: BaseHTTPRequestHandler) -> bool:
        auth = h.headers.get("authorization", "")
        given = h.headers.get("x-api-key") or (auth[7:] if auth.lower().startswith("bearer ") else None)
        return given == self.api_key

    def _models(self, h: BaseHTTPRequestHandler) -> None:
        if not self._authorized(h):
            return self._error(h, 401, "authentication_error", "invalid x-api-key")
        if h.path.split("?")[0] != "/v1/models":
            return self._error(h, 404, "not_found_error", f"unknown path {h.path}")
        data = [
            {"type": "model", "id": m, "display_name": m, "created_at": "2026-09-01T00:00:00Z"} for m in self.models
        ]
        return self._json(
            h, 200, {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}
        )

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        self.times.append(time.monotonic())
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n)
        # The API accepts the key as `x-api-key` or as `Authorization: Bearer` (newer SDKs send the latter).
        if not self._authorized(h):
            return self._error(h, 401, "authentication_error", "invalid x-api-key")
        try:
            body = json.loads(raw)
        except ValueError:
            return self._error(h, 400, "invalid_request_error", "body is not valid JSON")
        problem = self._validate(h, body)
        if problem:
            return self._error(h, 400, "invalid_request_error", problem)
        self.requests.append(body)
        fault = self.faults.pop(0) if self.faults else {}
        if "status" in fault:
            code = int(fault["status"])
            etype = fault.get("error_type") or {
                429: "rate_limit_error",
                529: "overloaded_error",
                500: "api_error",
                404: "not_found_error",
                401: "authentication_error",
            }.get(code, "invalid_request_error")
            return self._error(h, code, etype, fault.get("message", etype.replace("_", " ")), fault.get("headers"))
        if "delay" in fault:
            time.sleep(float(fault["delay"]))
        reply = self.responder(body)
        if body.get("stream"):
            return self._stream(h, body, reply, fault.get("stream_error"))
        return self._json(h, 200, self._message(body, reply))

    def _usage(self, body: dict[str, Any]) -> dict[str, int]:
        sysp = body.get("system")
        cached = isinstance(sysp, list) and any(isinstance(b, dict) and b.get("cache_control") for b in sysp)
        size = len(json.dumps(body)) // 4
        return {
            "input_tokens": size,
            "output_tokens": 0,
            "cache_creation_input_tokens": size // 2 if cached and len(self.requests) == 1 else 0,
            "cache_read_input_tokens": size // 2 if cached and len(self.requests) > 1 else 0,
        }

    def _blocks(self, body: dict[str, Any], reply: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        if "tool_input" in reply:
            name = (body.get("tool_choice") or {}).get("name") or body["tools"][0]["name"]
            return [
                {"type": "tool_use", "id": f"toolu_fake_{next(_ids)}", "name": name, "input": reply["tool_input"]}
            ], ("tool_use")
        return [{"type": "text", "text": reply.get("text", "")}], reply.get("stop_reason", "end_turn")

    def _message(self, body: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
        blocks, stop = self._blocks(body, reply)
        usage = self._usage(body)
        usage["output_tokens"] = max(1, len(json.dumps(blocks)) // 4)
        return {
            "id": f"msg_fake_{next(_ids)}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }

    def _stream(self, h: BaseHTTPRequestHandler, body: dict[str, Any], reply: dict[str, Any], err: str | None) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("request-id", f"req_fake_{next(_ids)}")
        h.end_headers()

        def ev(name: str, data: dict[str, Any]) -> None:
            h.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            h.wfile.flush()

        msg = self._message(body, reply)
        out_tokens = msg["usage"]["output_tokens"]
        start = dict(msg, content=[], stop_reason=None, usage=dict(msg["usage"], output_tokens=1))
        ev("message_start", {"type": "message_start", "message": start})
        if err:
            ev("error", {"type": "error", "error": {"type": err, "message": err.replace("_", " ")}})
            return
        ev("ping", {"type": "ping"})
        for i, block in enumerate(msg["content"]):
            if block["type"] == "text":
                ev(
                    "content_block_start",
                    {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                )
                text = block["text"]
                for k in range(0, len(text), 40):
                    delta = {"type": "text_delta", "text": text[k : k + 40]}
                    ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
            else:
                head = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": head})
                js = json.dumps(block["input"])
                for k in range(0, len(js), 25):
                    delta = {"type": "input_json_delta", "partial_json": js[k : k + 25]}
                    ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": out_tokens},
            },
        )
        ev("message_stop", {"type": "message_stop"})


def main() -> None:
    ap = argparse.ArgumentParser(description="Local fake of the Anthropic Messages API (for Hands dev/tests).")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--gadget", default="steelers", help="answer like the scripted model for this gadget")
    ap.add_argument("--api-key", default="test-key")
    a = ap.parse_args()
    fake = FakeAnthropic(gadget_responder(a.gadget), api_key=a.api_key)
    url = fake.start(a.port)
    print(f"fake Anthropic API on {url}  (x-api-key: {a.api_key})  Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
