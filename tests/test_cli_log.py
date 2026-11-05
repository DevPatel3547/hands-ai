"""CLI error handling, plugin registration and structured logging."""

from __future__ import annotations

import argparse
import json
import sys
import types

import pytest

from hands import cli
from hands.log import JsonFormatter, get_logger, redact, setup


def test_redact_hides_keys_and_tokens():
    s = "key sk-ant-api03-abcdefghijklmnop and Bearer abcdefgh12345678 and sk-proj1234567890abcdefgh"
    out = redact(s)
    assert "sk-ant" not in out and "abcdefgh1234" not in out and "proj1234" not in out
    assert out.count("[redacted]") == 3


def test_json_log_file(tmp_path):
    path = tmp_path / "log" / "hands.jsonl"
    setup(debug=False, json_path=str(path))
    get_logger("t").info("hello %s", "world", extra={"data": {"token": "sk-ant-api03-zzzzzzzzzzzz", "n": 1}})
    setup()  # close the file handler
    rec = json.loads(path.read_text().splitlines()[-1])
    assert rec["msg"] == "hello world" and rec["level"] == "info" and rec["logger"] == "hands.t"
    assert rec["data"] == {"token": "[redacted]", "n": 1}


def test_json_formatter_includes_exceptions():
    import logging

    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("hands", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exc"]


def test_errors_are_one_line_with_hint(monkeypatch, capsys):
    from hands.llm import LLMError

    def boom(a):
        raise LLMError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(cli, "_dispatch", boom)
    assert cli.main(["rules"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[0] == "error: ANTHROPIC_API_KEY is not set"
    assert err[1].startswith("hint: export ANTHROPIC_API_KEY")
    with pytest.raises(LLMError):  # --debug shows the real traceback
        cli.main(["--debug", "rules"])


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (type("BenchError", (Exception,), {})("x"), "hands doctor --real"),
        (type("SafetyRefusal", (Exception,), {})("x"), "low-voltage"),
        (type("DesignError", (Exception,), {})("x"), "hands check"),
        (FileNotFoundError("x"), "path"),
        (RuntimeError("x"), "--debug"),
    ],
)
def test_hints(exc, needle):
    assert needle in cli._hint_for(exc)


def test_plugins_register_and_missing_ones_are_skipped(monkeypatch, capsys):
    def register(sub):
        p = sub.add_parser("hello")
        p.set_defaults(func=lambda a: print("hi") or 0)

    monkeypatch.setitem(sys.modules, "hands.commands.fake", types.SimpleNamespace(register=register))
    monkeypatch.setattr(cli, "PLUGIN_MODULES", ("fake", "does_not_exist"))
    cli._PLUGIN_ERRORS.clear()
    assert cli.main(["hello"]) == 0
    assert capsys.readouterr().out.strip() == "hi"
    assert "does_not_exist" in cli._PLUGIN_ERRORS


def test_free_text_spec_needs_a_real_model(capsys):
    with pytest.raises(SystemExit, match="real model"):
        cli.main(["run", "a lamp that glows", "--sim"])


def test_parser_has_global_flags():
    a = cli.build_parser().parse_args(["--debug", "--log-json", "x.jsonl", "rules"])
    assert isinstance(a, argparse.Namespace) and a.debug and a.log_json == "x.jsonl"
