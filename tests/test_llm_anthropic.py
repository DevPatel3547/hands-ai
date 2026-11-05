"""The live Claude path, end to end through the real `anthropic` SDK against a local fake server
that speaks the Messages API wire format (JSON + SSE, tool use, errors, retry-after, timeouts)."""

from __future__ import annotations

import json

import pytest

anthropic = pytest.importorskip("anthropic")

from hands.llm import AnthropicClient, LLMError, ReplayClient, make_client  # noqa: E402
from tests.fakes.anthropic_server import FakeAnthropic, gadget_responder  # noqa: E402

KEY = "test-key"


@pytest.fixture
def fake():
    f = FakeAnthropic(api_key=KEY)
    url = f.start()
    f.url = url
    yield f
    f.stop()


def client(fake, **kw) -> AnthropicClient:
    kw.setdefault("max_retries", 3)
    return AnthropicClient("claude-sonnet-5-5", KEY, base_url=fake.url, **kw)


def test_text_stage_streams_and_records_usage(fake):
    fake.responder = lambda body: {"text": "```python\nprint('READY')\n```" * 5}
    seen: list[str] = []
    c = client(fake, on_text=seen.append)
    out = c.complete("sys prompt", "write firmware", stage="firmware")
    assert out == "```python\nprint('READY')\n```" * 5
    assert len(seen) > 1 and "".join(seen) == out  # really streamed in chunks
    req = fake.requests[-1]
    assert req["stream"] is True and req["model"] == "claude-sonnet-5-5"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in req
    call = c.calls[-1]
    assert call.stage == "firmware" and call.input_tokens > 0 and call.output_tokens > 0
    assert call.cache_write_tokens > 0 and call.stop_reason == "end_turn"
    c.complete("sys prompt", "again", stage="firmware")
    assert c.calls[-1].cache_read_tokens > 0


def test_json_stage_uses_forced_tool_with_schema(fake):
    design = {"name": "x", "parts": {"led1": "led_red"}, "nets": {"A": ["pico.GP15", "led1.anode"]}, "pin_map": {}}
    fake.responder = lambda body: {"tool_input": design}
    out = client(fake).complete("s", "p", stage="design")
    assert json.loads(out) == design  # assembled from input_json_delta chunks
    req = fake.requests[-1]
    assert req["tool_choice"] == {"type": "tool", "name": "submit_design"}
    schema = req["tools"][0]["input_schema"]
    assert schema["type"] == "object" and set(schema["required"]) == {"name", "parts", "nets", "pin_map"}


def test_unknown_json_stage_gets_free_object_schema(fake):
    fake.responder = lambda body: {"tool_input": {"leds": {"led1": "on"}}}
    out = client(fake).complete("s", "p", stage="vision_state")
    assert json.loads(out) == {"leds": {"led1": "on"}}
    assert fake.requests[-1]["tools"][0]["input_schema"] == {"type": "object"}


def test_429_and_529_are_retried_honouring_retry_after(fake):
    fake.faults = [
        {"status": 429, "headers": {"retry-after": "0.3"}},
        {"status": 529, "headers": {"retry-after": "0.2"}},
    ]
    out = client(fake).complete("s", "p", stage="firmware")
    assert out == "ok"
    assert len(fake.times) == 3
    assert fake.times[1] - fake.times[0] >= 0.28  # waited for retry-after, not the default backoff
    assert fake.times[2] - fake.times[1] >= 0.18


def test_rate_limit_exhausted_is_one_clean_error(fake):
    fake.faults = [{"status": 429, "headers": {"retry-after": "0"}}] * 5
    with pytest.raises(LLMError, match=r"rate limited .*HTTP 429"):
        client(fake, max_retries=2).complete("s", "p", stage="firmware")


def test_overloaded_exhausted(fake):
    fake.faults = [{"status": 529, "headers": {"retry-after": "0"}}] * 5
    with pytest.raises(LLMError, match="overloaded"):
        client(fake, max_retries=1).complete("s", "p", stage="firmware")


def test_mid_stream_overloaded_event_is_retried(fake):
    fake.faults = [{"stream_error": "overloaded_error"}]
    slept: list[float] = []
    out = client(fake, sleep=slept.append).complete("s", "p", stage="firmware")
    assert out == "ok" and slept == [2.0] and len(fake.requests) == 2


def test_mid_stream_overloaded_gives_up(fake):
    fake.faults = [{"stream_error": "overloaded_error"}] * 5
    with pytest.raises(LLMError, match="overloaded"):
        client(fake, sleep=lambda s: None).complete("s", "p", stage="firmware")


def test_timeout(fake):
    fake.faults = [{"delay": 1.5}, {"delay": 1.5}]
    with pytest.raises(LLMError, match="did not answer within 0.4 s"):
        client(fake, timeout=0.4, max_retries=1).complete("s", "p", stage="firmware")


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "rejected"), (404, "not found"), (400, "HTTP 400"), (500, "overloaded|HTTP 500")],
)
def test_status_errors_map_to_actionable_messages(fake, status, needle):
    fake.faults = [{"status": status}] * 6
    with pytest.raises(LLMError, match=needle):
        client(fake, max_retries=0, sleep=lambda s: None).complete("s", "p", stage="firmware")


def test_wrong_key_is_rejected_by_the_fake(fake):
    c = AnthropicClient("claude-sonnet-5-5", "wrong", base_url=fake.url, max_retries=0)
    with pytest.raises(LLMError, match="401"):
        c.complete("s", "p", stage="firmware")


def test_truncation_and_refusal(fake):
    fake.responder = lambda body: {"text": "partial", "stop_reason": "max_tokens"}
    with pytest.raises(LLMError, match="cut off"):
        client(fake).complete("s", "p", stage="firmware")
    fake.responder = lambda body: {"text": "", "stop_reason": "refusal"}
    with pytest.raises(LLMError, match="declined"):
        client(fake).complete("s", "p", stage="firmware")


def test_missing_tool_block(fake):
    fake.responder = lambda body: {"text": "I'd rather chat"}
    with pytest.raises(LLMError, match="structured result"):
        client(fake).complete("s", "p", stage="design")


def test_images_are_sent_as_base64_blocks(fake, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    client(fake).complete("s", "p", stage="firmware", images=[str(img)])
    content = fake.requests[-1]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"


def test_base_url_from_environment(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    c = make_client("anthropic:claude-haiku-5-5")
    assert c.complete("s", "p", stage="firmware") == "ok"
    assert fake.requests[-1]["model"] == "claude-haiku-5-5"


def test_full_demo_through_live_path_then_replay(fake, monkeypatch, tmp_path):
    """The whole Steelers loop with --model anthropic against the fake API, recorded, then replayed."""
    from hands.demo import run_sim_demo

    fake.responder = gadget_responder("steelers")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    rec = tmp_path / "steelers.jsonl"
    lines: list[str] = []
    res = run_sim_demo(
        "steelers",
        metrics_path=tmp_path / "m.jsonl",
        model="anthropic",
        base_url=fake.url,
        record=rec,
        log=lines.append,
    )
    assert res.success, res.reason
    stages = [r.get("tools", [{}])[0].get("name", "firmware") for r in fake.requests]
    assert stages[:2] == ["submit_design", "submit_design_revise"] and "submit_test_plan" in stages
    header, *calls = [json.loads(x) for x in rec.read_text().splitlines()]
    assert header["format"] == "hands-replay/1" and header["model"] == "claude-sonnet-5-5"
    assert [c["stage"] for c in calls][:2] == ["design", "design_revise"]
    m = [json.loads(x) for x in (tmp_path / "m.jsonl").read_text().splitlines()]
    summary = next(e for e in m if e["event"] == "run_end")["metrics"]
    assert summary["input_tokens"] > 0 and summary["model_source"] == "live"

    n_before = len(fake.requests)
    lines.clear()
    res2 = run_sim_demo("steelers", metrics_path=tmp_path / "m2.jsonl", model=f"replay:{rec}", log=lines.append)
    assert res2.success and len(fake.requests) == n_before  # no network
    assert any("REPLAY of claude-sonnet-5-5" in x for x in lines)


def test_replay_errors(tmp_path):
    with pytest.raises(LLMError, match="not found"):
        ReplayClient(tmp_path / "nope.jsonl")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"x": 1}\n')
    with pytest.raises(LLMError, match="not a hands-replay"):
        ReplayClient(bad)
    ok = tmp_path / "ok.jsonl"
    ok.write_text('{"format": "hands-replay/1", "model": "m", "recorded": "t"}\n{"stage": "design", "reply": "{}"}\n')
    r = ReplayClient(ok)
    assert r.complete("s", "p", stage="design") == "{}"
    with pytest.raises(LLMError, match="diverged"):
        r.complete("s", "p", stage="design")
    with pytest.raises(LLMError, match="real model"):
        make_client("replay:" + str(ok), record=tmp_path / "x.jsonl")
    with pytest.raises(LLMError, match="needs a file"):
        make_client("replay")


def test_doctor_live_verifies_key_with_free_call(fake):
    from hands.doctor import Env, exit_code, render, run_checks

    env = Env(environ={"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": fake.url})
    checks = run_checks(env, mode="live", plugins=False)
    text = render(checks)
    assert "key works" in text and "3 models visible" in text and KEY not in text
    assert "WARN" not in [c.status for c in checks if c.name.startswith("model claude")]
    assert exit_code(checks) == 0
    assert fake.requests == []  # no Messages call: nothing billed

    bad = Env(environ={"ANTHROPIC_API_KEY": "sk-ant-wrong-but-well-formed", "ANTHROPIC_BASE_URL": fake.url})
    checks = run_checks(bad, mode="live", plugins=False)
    assert exit_code(checks) == 1 and "sk-ant-wrong" not in render(checks)
    other = Env(environ={"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": fake.url, "HANDS_MODEL": "claude-x"})
    assert "set HANDS_MODEL" in render(run_checks(other, mode="live", plugins=False))


def test_doctor_live_without_key_and_platform_lines():
    from hands.doctor import Env, exit_code, render, run_checks

    env = Env(environ={}, system="Darwin", machine="arm64", which=lambda x: None)
    text = render(run_checks(env, mode="live", plugins=False))
    assert "macOS Apple Silicon" in text and "Homebrew" in text and "export ANTHROPIC_API_KEY" in text
    assert exit_code(run_checks(env, mode="live", plugins=False)) == 1
