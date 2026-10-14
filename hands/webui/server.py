"""Local step-by-step wiring UI (stdlib http.server, no dependencies).

GET  /                         page (static/index.html + app.js + app.css)
GET  /api/state                steps, progress, photo/self-check status, power-on readiness
GET  /api/steps/<n>/svg        breadboard diagram up to step n
POST /api/steps/<n>/done|undo  mark a step done / not done
POST /api/photo                upload a photo (raw JPEG/PNG/WebP body, e.g. from a phone) and check it
POST /api/photo_check          take a photo with the configured camera and check it
POST /api/selfcheck            {"items": [ids]}: the person ticked these parts/wires as checked by eye

Power-on is "ready" only when every step is done AND the latest photo check passed (or, with
no vision model, every self-check item is ticked). The page shows a large warning until then.

Security: binds to 127.0.0.1 by default. With ``--host 0.0.0.0`` (to use a phone on the same
Wi-Fi) every API call needs the random token printed in the URL. Uploads are size-limited and
must be real JPEG/PNG/WebP images; model/vision text is never inserted into the page as HTML.
"""

from __future__ import annotations

import inspect
import json
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..wiring import Guide, ascii_diagram, svg_diagram

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

#: photo_check(path) -> list of discrepancy dicts. path=None means "use the server's camera".
PhotoCheck = Callable[[str | None], list[dict[str, Any]]]


def image_kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class GuideState:
    def __init__(self, guide: Guide, photo_check: PhotoCheck | None, has_camera: bool, upload_dir: Path):
        self.guide = guide
        self.photo_check = photo_check
        self.has_camera = has_camera
        self.upload_dir = upload_dir
        self.lock = threading.Lock()
        self.photo_ok = False
        self.last_photo: dict[str, Any] | None = None
        self.selfcheck: set[str] = set()

    def selfcheck_items(self) -> list[dict[str, str]]:
        L = self.guide.layout
        items = [
            {"id": f"part:{i}", "text": f"{i}: " + ", ".join(f"{p} in {h}" for p, h in pl.holes.items())}
            for i, pl in L.placements.items()
            if pl.holes
        ]
        items += [{"id": f"wire:{w.id}", "text": f"Wire {w.id}: {w.a} to {w.b}, pushed fully in"} for w in L.wires]
        items.append({"id": "rails", "text": "Nothing bare touches across the red (+) and blue (-) rails"})
        return items

    def power(self) -> dict[str, Any]:
        reasons = []
        steps = self.guide.steps
        left = [s.n for s in steps if not s.done]
        if left:
            reasons.append(f"{len(left)} wiring step(s) not done yet")
        ids = {x["id"] for x in self.selfcheck_items()}
        verified = self.photo_ok or (self.photo_check is None and ids <= self.selfcheck)
        if not verified:
            reasons.append(
                "the photo check has not passed" if self.photo_check else "tick every item in the self-check list"
            )
        return {"ready": not reasons, "reasons": reasons}

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.guide.steps],
            "camera": self.has_camera,
            "vision": self.photo_check is not None,
            "photo_ok": self.photo_ok,
            "last_photo": self.last_photo,
            "selfcheck_items": self.selfcheck_items(),
            "selfcheck_done": sorted(self.selfcheck),
            "power": self.power(),
        }

    def run_photo_check(self, path: str | None) -> dict[str, Any]:
        if self.photo_check is None:
            return {
                "ok": False,
                "error": "photo checking needs a vision model (start with --vision-model); "
                "use the self-check list instead",
            }
        try:
            diffs = self.photo_check(path)
        except Exception as e:  # noqa: BLE001 - shown to the user, never crashes the server
            return {"ok": False, "error": str(e)[:300]}
        with self.lock:
            self.photo_ok = not diffs
            self.last_photo = {"ok": not diffs, "discrepancies": diffs, "t": time.time()}
        return {"ok": not diffs, "discrepancies": diffs}


def make_handler(state: GuideState, token: str | None) -> type[BaseHTTPRequestHandler]:
    guide = state.guide

    class H(BaseHTTPRequestHandler):
        server_version = "hands"
        sys_version = ""

        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authorized(self) -> bool:
            if token is None:
                return True
            q = parse_qs(urlparse(self.path).query)
            got = self.headers.get("X-Hands-Token") or (q.get("t") or [""])[0]
            return secrets.compare_digest(got, token)

        def _route(self) -> list[str]:
            return urlparse(self.path).path.strip("/").split("/")

        def _step(self, parts: list[str]) -> Any:
            try:
                return guide.steps[int(parts[2])]
            except (ValueError, IndexError):
                return None

        def _body(self, limit: int) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n <= 0 or n > limit:
                return None
            return self.rfile.read(n)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            if not self._authorized():
                return self._json({"error": "missing or wrong token: open the exact URL Hands printed"}, 403)
            parts = self._route()
            if parts == ["api", "state"]:
                with state.lock:
                    return self._json(state.snapshot())
            if parts == ["api", "steps"]:  # backwards compatible
                return self._json({"steps": [s.to_dict() for s in guide.steps], "camera": state.has_camera})
            if parts[:2] == ["api", "steps"] and len(parts) == 4 and parts[3] in ("svg", "ascii"):
                s = self._step(parts)
                if s is None:
                    return self._json({"error": "no such step"}, 404)
                if parts[3] == "svg":
                    return self._send(200, svg_diagram(guide.layout, s.n, guide.steps).encode(), "image/svg+xml")
                return self._send(
                    200, ascii_diagram(guide.layout, s.n, guide.steps).encode(), "text/plain; charset=utf-8"
                )
            if parts == ["api", "power"]:
                with state.lock:
                    return self._json(state.power())
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json({"error": "missing or wrong token"}, 403)
            parts = self._route()
            if parts[:2] == ["api", "steps"] and len(parts) == 4 and parts[3] in ("done", "undo"):
                s = self._step(parts)
                if s is None:
                    return self._json({"error": "no such step"}, 404)
                with state.lock:
                    s.done = parts[3] == "done"
                    state.photo_ok = False  # any change to the build invalidates the last photo check
                return self._json({"ok": True, "step": s.n, "done": s.done})
            if parts == ["api", "photo_check"]:
                if not state.has_camera:
                    return self._json(
                        {"ok": False, "error": "no camera configured (start with --camera, or upload a photo instead)"}
                    )
                return self._json(state.run_photo_check(None))
            if parts == ["api", "photo"]:
                data = self._body(MAX_UPLOAD)
                if data is None:
                    return self._json(
                        {"ok": False, "error": f"photo missing or larger than {MAX_UPLOAD >> 20} MB"}, 413
                    )
                kind = image_kind(data)
                if kind is None:
                    return self._json({"ok": False, "error": "please upload a JPEG, PNG or WebP photo"}, 415)
                p = state.upload_dir / f"upload_{int(time.time() * 1000)}.{kind}"
                p.write_bytes(data)
                res = state.run_photo_check(str(p))
                res["saved"] = p.name
                return self._json(res)
            if parts == ["api", "selfcheck"]:
                raw = self._body(64 * 1024)
                try:
                    items = json.loads(raw or b"{}").get("items", [])
                except (json.JSONDecodeError, AttributeError):
                    return self._json({"error": "bad JSON"}, 400)
                valid = {x["id"] for x in state.selfcheck_items()}
                with state.lock:
                    state.selfcheck = {str(i) for i in items if str(i) in valid}
                    return self._json({"ok": True, "power": state.power()})
            self._json({"error": "not found"}, 404)

    return H


def make_server(
    guide: Guide,
    port: int = 8765,
    photo_check: Any = None,
    host: str = "127.0.0.1",
    has_camera: bool | None = None,
    upload_dir: str | Path | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """``photo_check`` may take an optional image path (``f(path)``) or no argument (``f()``, legacy)."""
    pc: PhotoCheck | None = None
    if photo_check is not None:
        takes_path = bool(inspect.signature(photo_check).parameters)

        def pc(path: str | None, _f: Any = photo_check) -> list[dict[str, Any]]:
            res: list[dict[str, Any]] = _f(path) if takes_path else _f()
            return res

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if token is None and not loopback:
        token = secrets.token_urlsafe(16)
    state = GuideState(
        guide,
        pc,
        bool(photo_check) if has_camera is None else has_camera,
        Path(upload_dir or tempfile.mkdtemp(prefix="hands-photos-")),
    )
    srv = ThreadingHTTPServer((host, port), make_handler(state, token))
    srv.hands_token = token  # type: ignore[attr-defined]
    srv.hands_state = state  # type: ignore[attr-defined]
    return srv


def serve(
    guide: Guide, port: int = 8765, photo_check: Any = None, host: str = "127.0.0.1", has_camera: bool | None = None
) -> None:
    srv = make_server(guide, port, photo_check, host, has_camera)
    tok = getattr(srv, "hands_token", None)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104 - display only
    url = f"http://{shown}:{srv.server_address[1]}/" + (f"?t={tok}" if tok else "")
    print(f"Hands wiring guide on {url}  (Ctrl-C to stop)")
    if tok:
        print(
            "   On your phone (same Wi-Fi), open the same URL with this computer's local IP address instead of "
            "127.0.0.1. Keep the token secret."
        )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
