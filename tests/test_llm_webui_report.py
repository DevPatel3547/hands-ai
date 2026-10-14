"""Model adapters, web UI, study report, CLI surface."""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hands.cli import main
from hands.layout import place
from hands.llm import LLMError, OpenAICompatClient, ScriptedClient, make_client, parse_model_spec
from hands.metrics import Metrics, session_rows, study_report
from hands.vision import ConsoleVision, ModelVision, sanitize_wiring
from hands.wiring import build_guide, expected_observation

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ------------------------------------------------------------------ models ---


class FakeOpenAI(BaseHTTPRequestHandler):
    seen: list = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.seen.append((self.path, dict(self.headers), body))
        if FakeOpenAI.fail_first > 0:
            FakeOpenAI.fail_first -= 1
            self.send_response(503)
            self.end_headers()
            return
        out = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def fake_openai():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeOpenAI.seen, FakeOpenAI.fail_first = [], 0
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_openai_compatible_adapter_against_local_server(fake_openai, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(PNG)
    c = OpenAICompatClient("qwen2.5-coder:7b", fake_openai, api_key="k", sleep=lambda s: None)
    assert c.complete("sys", "hi", stage="design", images=[str(img)]) == '{"ok": true}'
    path, headers, body = FakeOpenAI.seen[-1]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert body["response_format"] == {"type": "json_object"} and body["model"] == "qwen2.5-coder:7b"
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    c.complete("sys", "fw", stage="firmware")
    assert "response_format" not in FakeOpenAI.seen[-1][2]
    FakeOpenAI.fail_first = 2  # 503 twice, then success: retried
    assert c.complete("s", "p", stage="diagnose") == '{"ok": true}'
    assert len(c.calls) == 3


def test_openai_adapter_errors(tmp_path):
    with pytest.raises(LLMError, match="http"):
        OpenAICompatClient("m", "ftp://x")

    def down(req, timeout):
        raise urllib.error.URLError("refused")

    c = OpenAICompatClient("m", "http://127.0.0.1:9", opener=down, sleep=lambda s: None, retries=2)
    with pytest.raises(LLMError, match="ollama serve"):
        c.complete("s", "p", stage="design")

    def bad_json(req, timeout):
        return b'{"nope": 1}'

    with pytest.raises(LLMError, match="unexpected response"):
        OpenAICompatClient("m", "http://x", opener=bad_json).complete("s", "p", stage="x")

    def forbidden(req, timeout):
        raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)

    with pytest.raises(LLMError, match="401"):
        OpenAICompatClient("m", "http://x", opener=forbidden).complete("s", "p", stage="x")
    txt = tmp_path / "t.txt"
    txt.write_text("not an image")
    with pytest.raises(LLMError, match="JPEG"):
        OpenAICompatClient("m", "http://x", opener=bad_json).complete("s", "p", stage="x", images=[str(txt)])


def test_model_spec_and_factory(monkeypatch):
    assert parse_model_spec("ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")
    assert parse_model_spec("scripted") == ("scripted", None)
    with pytest.raises(LLMError):
        parse_model_spec("gpt5")
    s = ScriptedClient({})
    assert make_client("scripted", scripted=s) is s
    with pytest.raises(LLMError, match="built-in gadgets"):
        make_client("scripted")
    c = make_client("ollama")
    assert isinstance(c, OpenAICompatClient) and c.base_url.endswith("11434/v1")
    c = make_client("openai:gpt-4o-mini", base_url="http://localhost:1234/v1")
    assert c.model == "gpt-4o-mini" and c.base_url == "http://localhost:1234/v1"
    with pytest.raises(LLMError, match="needs a model"):
        make_client("openai")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        make_client("anthropic")


def test_sim_demo_runs_with_ollama_model_spec(fake_openai, tmp_path, monkeypatch):
    """A non-scripted model that returns garbage never gets past the rule gate (and nothing is powered)."""
    from hands.demo import run_sim_demo

    res = run_sim_demo(
        "steelers",
        faults=[],
        metrics_path=tmp_path / "m.jsonl",
        log=lambda s: None,
        model="ollama:fake",
        base_url=fake_openai,
    )
    assert not res.success and "safety rules" in res.reason


# ------------------------------------------------------------------ vision ---


def test_vision_output_is_sanitized(inv, steelers_design):
    layout = place(steelers_design, inv)
    good = expected_observation(layout)
    s = sanitize_wiring(dict(good, wires=good["wires"] + [["<script>", "a1"], ["a1"], "x"]))
    assert s["wires"] == good["wires"]
    assert sanitize_wiring("garbage")["uncertain"]
    vis = ModelVision(ScriptedClient({"vision_wiring": ["not json"], "vision_state": ['{"leds": {"led1": "on"}}']}))
    from hands.bench.base import Snapshot

    assert vis.observe_wiring(Snapshot(0, "/x.jpg"), layout)["wires"] == []
    assert vis.observe_state(Snapshot(0, "/x.jpg"), ["led1"]) == {"leds": {"led1": "on"}}
    with pytest.raises(ValueError):
        vis.observe_wiring(Snapshot(0, None), layout)


def test_console_vision(inv, steelers_design):
    layout = place(steelers_design, inv)
    answers = iter(["n"] + ["y"] * 200)
    cv = ConsoleVision(lambda q: next(answers))
    obs = cv.observe_wiring(None, layout)
    from hands.wiring import compare

    diffs = compare(layout, obs)
    assert len(diffs) == 1 and diffs[0].kind == "part_missing"
    ans2 = iter(["y", "Temp: 22.1 C", "90", "blinking", "maybe"])
    cv2 = ConsoleVision(lambda q: next(ans2))
    st = cv2.observe_state(None, ["led1", "oled1", "servo1"])
    assert st == {"leds": {"led1": "on"}, "oled": {"oled1": ["Temp: 22.1 C"]}, "servo_deg": {"servo1": 90.0}}
    assert cv2.observe_led_pattern("led1") == "blinking" and cv2.observe_led_pattern("led1") == "off"


def test_real_run_with_console_human_and_fake_bench(inv, tmp_path):
    """`hands run steelers --real` wiring: ConsoleHuman + ConsoleVision on a (simulated) bench."""
    from hands.bench.sim_bench import SimBench
    from hands.demo import run_real
    from hands.sim.build import Build

    bench = SimBench(inv, Build())
    with pytest.raises(SystemExit):  # free-form spec + scripted model is not possible
        run_real("make a lamp", model="scripted", bench=bench, ask=lambda q: "y", log=lambda s: None)
    # with a person answering "y" to every check but nothing actually wired, the smoke test passes (0 mA) and
    # the first functional test must fail: the loop stops safely instead of claiming success.
    res = run_real("steelers", bench=bench, ask=lambda q: "y", log=lambda s: None, metrics_path=tmp_path / "m.jsonl")
    assert not res.success


# ------------------------------------------------------------------ web UI ---


def _req(base, path, method="GET", data=None, headers=None):
    req = urllib.request.Request(base + path, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture
def web(inv, steelers_design, tmp_path):
    from hands.webui.server import make_server

    guide = build_guide(place(steelers_design, inv), inv)
    calls = []

    def photo(path):
        calls.append(path)
        return [] if len(calls) > 1 else [{"message": "<b>r1</b> missing", "instruction": "insert r1"}]

    srv = make_server(guide, port=0, photo_check=photo, upload_dir=tmp_path, has_camera=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", guide, calls, srv
    srv.shutdown()


def test_web_ui_flow_gates_power(web):
    base, guide, calls, srv = web
    code, headers, html = _req(base, "/")
    assert code == 200 and b"Hands" in html and "default-src 'self'" in headers["Content-Security-Policy"]
    assert b"<script>" not in html.replace(b'<script src="/app.js"></script>', b"")
    for f in ("/app.js", "/app.css"):
        assert _req(base, f)[0] == 200
    st = json.loads(_req(base, "/api/state")[2])
    assert st["power"]["ready"] is False and st["vision"] is True
    for s in guide.steps:
        assert json.loads(_req(base, f"/api/steps/{s.n}/done", "POST")[2])["ok"]
    st = json.loads(_req(base, "/api/power")[2])
    assert not st["ready"] and "photo check" in st["reasons"][0]
    code, _, body = _req(base, "/api/photo", "POST", b"not an image", {"Content-Type": "image/jpeg"})
    assert code == 415
    r = json.loads(_req(base, "/api/photo", "POST", PNG, {"Content-Type": "image/png"})[2])
    assert r["ok"] is False and r["discrepancies"][0]["message"].startswith("<b>")  # returned as data, rendered as text
    assert calls[-1].endswith(".png")
    r = json.loads(_req(base, "/api/photo_check", "POST")[2])
    assert r["ok"] is True and calls[-1] is None
    assert json.loads(_req(base, "/api/power")[2])["ready"] is True
    _req(base, "/api/steps/3/undo", "POST")  # any change invalidates the photo check
    assert json.loads(_req(base, "/api/power")[2])["ready"] is False
    assert _req(base, "/api/steps/999/done", "POST")[0] == 404
    assert _req(base, "/api/steps/2/svg")[2].startswith(b"<svg")
    assert _req(base, "/api/steps/2/ascii")[0] == 200
    assert _req(base, "/api/nope")[0] == 404
    assert _req(base, "/api/photo", "POST", b"", {"Content-Type": "image/png"})[0] == 413


def test_web_ui_selfcheck_without_vision_and_token(inv, steelers_design, tmp_path):
    from hands.webui.server import make_server

    guide = build_guide(place(steelers_design, inv), inv)
    srv = make_server(guide, port=0, host="127.0.0.1", token="secret", upload_dir=tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert _req(base, "/")[0] == 200  # the page itself is public, the API is not
        assert _req(base, "/api/state")[0] == 403
        h = {"X-Hands-Token": "secret"}
        st = json.loads(_req(base, "/api/state", headers=h)[2])
        assert st["vision"] is False and _req(base, "/api/state?t=secret")[0] == 200
        assert json.loads(_req(base, "/api/photo_check", "POST", headers=h)[2])["ok"] is False
        for s in guide.steps:
            _req(base, f"/api/steps/{s.n}/done", "POST", headers=h)
        ids = [x["id"] for x in st["selfcheck_items"]]
        r = json.loads(
            _req(
                base,
                "/api/selfcheck",
                "POST",
                json.dumps({"items": ids[:-1] + ["bogus"]}).encode(),
                dict(h, **{"Content-Type": "application/json"}),
            )[2]
        )
        assert r["power"]["ready"] is False
        r = json.loads(_req(base, "/api/selfcheck", "POST", json.dumps({"items": ids}).encode(), h)[2])
        assert r["power"]["ready"] is True
        assert _req(base, "/api/selfcheck", "POST", b"{nope", h)[0] == 400
        assert _req(base, "/api/steps", headers=h)[0] == 200
    finally:
        srv.shutdown()
    srv2 = make_server(guide, port=0, host="0.0.0.0")
    assert srv2.hands_token and len(srv2.hands_token) >= 16
    srv2.server_close()


# ------------------------------------------------------------------ report ---


def test_study_report(tmp_path):
    from hands.demo import run_sim_demo

    log = tmp_path / "m.jsonl"
    run_sim_demo("steelers", metrics_path=log, log=lambda s: None, participant="P01")
    m = Metrics(log, gadget="tempdisplay", mode="real+scripted", participant="P02")
    m.event("run_start", spec="x")
    m.event("intervention", phase="pre_power", reason="led1 is in backwards")
    m.event("smoke_test", attempt=1, error="board not responding")
    m.event("power_cut", reason="x")
    m.event(
        "run_end",
        success=True,
        reason="",
        metrics={
            "time_to_working_s": 1800,
            "errors_caught_pre_power": 1,
            "errors_caught_post_power": 1,
            "interventions": 2,
        },
    )
    Metrics(log, gadget="servo", mode="real+scripted", participant="P03").event("run_start")
    with log.open("a") as f:
        f.write("{corrupt line\n")
    rows = session_rows(log)
    assert len(rows) == 3 and {r["result"] for r in rows} == {"WORKING", "INCOMPLETE"}
    md = study_report(log, "Pilot")
    assert md.startswith("# Pilot") and "Real-hardware sessions" in md and "Simulated sessions" in md
    assert "| P02 | tempdisplay |" in md and "30.0" in md and "1 failed smoke test" in md
    assert "% caught before any power was applied" in md
    assert main(["report", "--log", str(log), "--format", "md", "--out", str(tmp_path / "r.md")]) == 0
    assert (tmp_path / "r.md").read_text().startswith("# Hands study report")
    events = [json.loads(x) for x in log.read_text().splitlines() if x.startswith('{"')]
    assert all(e["schema"] == 1 and e["ts"].endswith("Z") for e in events)


# ------------------------------------------------------------------ CLI ---


def test_cli_surface(tmp_path, capsys, monkeypatch):
    assert main(["check", "steelers", "--final", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    d = tmp_path / "d.json"
    d.write_text(
        json.dumps(
            {
                "parts": {"led1": "led_red"},
                "nets": {"A": ["pico.GP2", "led1.anode"], "GND": ["led1.cathode"]},
                "pin_map": {"L": "GP2"},
            }
        )
    )
    assert main(["guide", str(d)]) == 1
    assert "Refusing" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["check", "no-such-thing"])
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    assert "overvoltage" in out and "gpio_max_ma" in out
    assert main(["inventory"]) == 0
    assert main(["run", "servo", "--sim", "--faults", "none", "--metrics", str(tmp_path / "m.jsonl")]) == 0
    assert main(["doctor", "--sim"]) == 0
    assert "core" in capsys.readouterr().out
    assert main(["doctor", "--sim", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"].startswith("Python")
    assert main(["bridge", "--feed", "mock"]) == 0
    assert "SCORE 7 17" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--version"])
