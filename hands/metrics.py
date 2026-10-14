"""Structured JSONL session logs + reports.

Every event is one JSON object per line (schema 1)::

    {"schema": 1, "run_id": "...", "gadget": "steelers", "mode": "real+ollama", "participant": "P03",
     "ts": "2026-10-11T18:02:11.123Z", "t_wall": 412.5, "event": "intervention", ...}

Event kinds: ``run_start``, ``refused``, ``rule_check``, ``flash``, ``wiring_step``, ``wiring_done``,
``photo_check``, ``intervention`` (phase ``pre_power``/``post_power``), ``power_on``, ``power_cut``,
``smoke_test``, ``firmware_lint``, ``test_plan``, ``test``, ``diagnosis``, ``bringup_step``, ``run_end``.

``hands report`` prints a terminal table; ``hands report --format md`` writes a markdown study
report (see ``docs/study-protocol.md``).
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG = Path("metrics") / "runs.jsonl"
SCHEMA = 1


class Metrics:
    def __init__(
        self, path: str | Path | None = DEFAULT_LOG, gadget: str = "", mode: str = "sim", participant: str = ""
    ):
        self.path = Path(path) if path else None
        self.run_id = uuid.uuid4().hex[:10]
        self.gadget, self.mode, self.participant = gadget, mode, participant
        self.t0 = time.time()
        self.events: list[dict[str, Any]] = []
        self.counters: defaultdict[str, float] = defaultdict(float)

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        now = time.time()
        e = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "gadget": self.gadget,
            "mode": self.mode,
            "participant": self.participant,
            "ts": datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "t_wall": round(now - self.t0, 3),
            "event": kind,
            **data,
        }
        self.events.append(e)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(e, default=str) + "\n")
        return e

    def inc(self, key: str, n: float = 1) -> None:
        self.counters[key] += n

    def elapsed(self) -> float:
        return time.time() - self.t0


def load_runs(path: str | Path = DEFAULT_LOG) -> dict[str, list[dict[str, Any]]]:
    """Group events by run. Corrupt lines are skipped (a crash mid-write must not lose the study)."""
    runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    p = Path(path)
    if not p.exists():
        return {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and "run_id" in e:
            runs[e["run_id"]].append(e)
    return dict(runs)


def session_rows(path: str | Path = DEFAULT_LOG) -> list[dict[str, Any]]:
    rows = []
    for rid, evs in load_runs(path).items():
        first = evs[0]
        end = next((e for e in reversed(evs) if e["event"] == "run_end"), None)
        m = end["metrics"] if end else {}
        interventions = [e for e in evs if e["event"] == "intervention"]
        rows.append(
            {
                "run": rid,
                "participant": first.get("participant", "") or "-",
                "gadget": first.get("gadget", "?"),
                "mode": first.get("mode", "?"),
                "result": "INCOMPLETE" if end is None else ("WORKING" if end.get("success") else "FAILED"),
                "reason": (end or {}).get("reason", ""),
                "ttw_min": round(m["time_to_working_s"] / 60, 1) if m.get("time_to_working_s") is not None else None,
                "wiring_steps": int(m.get("wiring_steps", 0)),
                "design_errors": int(m.get("design_errors_caught", 0)),
                "pre_power": int(m.get("errors_caught_pre_power", 0)),
                "post_power": int(m.get("errors_caught_post_power", 0)),
                "interventions": int(m.get("interventions", len(interventions))),
                "debug_rounds": int(m.get("debug_rounds", 0)),
                "llm_calls": int(m.get("llm_calls", 0)),
                "lint_rejections": int(m.get("firmware_lint_rejections", 0)),
                "refused": any(e["event"] == "refused" for e in evs),
                "smoke_fails": sum(
                    1
                    for e in evs
                    if e["event"] == "smoke_test" and ("error" in e or e.get("max_ma", 0) > e.get("budget_ma", 1e9))
                ),
                "power_cuts": sum(1 for e in evs if e["event"] == "power_cut"),
                "start": first.get("ts", ""),
                "intervention_log": [(e.get("phase", "?"), e.get("reason", "")) for e in interventions],
            }
        )
    return rows


def summary_report(path: str | Path = DEFAULT_LOG) -> str:
    rows = session_rows(path)
    if not rows:
        return f"No runs logged in {path}."
    cols = [
        ("run", "run"),
        ("participant", "who"),
        ("gadget", "gadget"),
        ("mode", "mode"),
        ("result", "result"),
        ("ttw_min", "time-to-working"),
        ("debug_rounds", "debug rds"),
        ("design_errors", "design errs"),
        ("pre_power", "pre-power"),
        ("post_power", "post-power"),
        ("llm_calls", "LLM calls"),
    ]
    table = [[("-" if r[k] is None else f"{r[k]} min" if k == "ttw_min" else str(r[k])) for k, _ in cols] for r in rows]
    hdr = [h for _, h in cols]
    widths = [max(len(x[i]) for x in table + [hdr]) for i in range(len(hdr))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*hdr), fmt.format(*["-" * w for w in widths])] + [fmt.format(*t) for t in table]
    ok = [r for r in rows if r["result"] == "WORKING"]
    lines += [
        "",
        f"{len(rows)} run(s), {len(ok)} reached a working device. "
        f"Errors caught before power-on: {sum(r['pre_power'] for r in rows)}; "
        f"after power-on: {sum(r['post_power'] for r in rows)}.",
        "Sim runs: time-to-working = estimated beginner time for each action + simulated bench time.",
    ]
    return "\n".join(lines)


def _stats(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    if len(xs) == 1:
        return f"{xs[0]:.1f}"
    q = statistics.quantiles(xs, n=4) if len(xs) >= 2 else [xs[0]] * 3
    return f"median {statistics.median(xs):.1f} (IQR {q[0]:.1f}-{q[2]:.1f}, range {min(xs):.1f}-{max(xs):.1f})"


def _section(rows: list[dict[str, Any]], title: str) -> list[str]:
    out = [f"## {title}", ""]
    if not rows:
        return out + ["No sessions.", ""]
    n = len(rows)
    ok = [r for r in rows if r["result"] == "WORKING"]
    pre, post = sum(r["pre_power"] for r in rows), sum(r["post_power"] for r in rows)
    caught = pre + post
    out += [
        f"- Sessions: **{n}** ({len({r['participant'] for r in rows})} participant id(s))",
        f"- Reached a working device: **{len(ok)}/{n}** ({100 * len(ok) / n:.0f}%)",
        f"- Time to working device, minutes: {_stats([r['ttw_min'] for r in ok if r['ttw_min'] is not None])}",
        f"- Wiring/assembly mistakes caught: **{caught}** total; **{pre}** before power-on (photo check), "
        f"**{post}** after power-on (smoke test / instruments)"
        + (f" -> {100 * pre / caught:.0f}% caught before any power was applied" if caught else ""),
        f"- Design errors caught by the rule checker (before any wiring): {sum(r['design_errors'] for r in rows)}",
        f"- Human interventions (fix instructions) per session: {_stats([float(r['interventions']) for r in rows])}",
        f"- Debug rounds per session: {_stats([float(r['debug_rounds']) for r in rows])}",
        f"- Safety events: {sum(r['refused'] for r in rows)} refused spec(s), "
        f"{sum(r['smoke_fails'] for r in rows)} failed smoke test(s), {sum(r['power_cuts'] for r in rows)} power cut(s), "
        f"{sum(r['lint_rejections'] for r in rows)} firmware lint rejection(s)",
        "",
        "| run | participant | gadget | mode | result | min to working | steps | design errs | pre-power | "
        "post-power | interventions | debug rds | LLM calls |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| `{r['run']}` | {r['participant']} | {r['gadget']} | {r['mode']} | {r['result']} | "
            f"{'-' if r['ttw_min'] is None else r['ttw_min']} | {r['wiring_steps']} | {r['design_errors']} | "
            f"{r['pre_power']} | {r['post_power']} | {r['interventions']} | {r['debug_rounds']} | "
            f"{r['llm_calls']} |"
        )
    out.append("")
    by_g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_g[r["gadget"]].append(r)
    if len(by_g) > 1:
        out += [
            "| gadget | sessions | working | median min to working | mistakes pre / post power |",
            "|---|---|---|---|---|",
        ]
        for g, rs in sorted(by_g.items()):
            t = [r["ttw_min"] for r in rs if r["result"] == "WORKING" and r["ttw_min"] is not None]
            out.append(
                f"| {g} | {len(rs)} | {sum(r['result'] == 'WORKING' for r in rs)} | "
                f"{statistics.median(t):.1f} | {sum(r['pre_power'] for r in rs)} / "
                f"{sum(r['post_power'] for r in rs)} |"
                if t
                else f"| {g} | {len(rs)} | 0 | - | {sum(r['pre_power'] for r in rs)} / {sum(r['post_power'] for r in rs)} |"
            )
        out.append("")
    notes = [r for r in rows if r["intervention_log"] or r["result"] != "WORKING"]
    if notes:
        out += ["<details><summary>Interventions and failures per session</summary>", ""]
        for r in notes:
            out.append(
                f"- `{r['run']}` ({r['participant']}, {r['gadget']}): {r['result']}"
                + (f" - {r['reason'][:160]}" if r["reason"] else "")
            )
            for phase, why in r["intervention_log"][:12]:
                out.append(f"  - {phase}: {why[:140]}")
        out += ["", "</details>", ""]
    return out


def study_report(path: str | Path = DEFAULT_LOG, title: str = "Hands study report") -> str:
    """Markdown report for a small non-expert user study. Real (human) and simulated sessions are kept apart."""
    rows = session_rows(path)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# {title}", "", f"Generated {now} from `{path}` ({len(rows)} session(s)).", ""]
    real = [r for r in rows if str(r["mode"]).startswith("real")]
    sim = [r for r in rows if not str(r["mode"]).startswith("real")]
    out += _section(real, "Real-hardware sessions (people)")
    out += _section(sim, "Simulated sessions (scripted human, for reference only)")
    out += [
        "## How to read this",
        "",
        "- *Time to working device* is wall-clock time from the start of the session to the last passing test "
        "(real), or the sum of per-action beginner-time estimates plus simulated bench time (sim).",
        "- *Before power-on* = caught by the photo check while USB was unplugged. *After power-on* = caught by the "
        "INA219 smoke test, the logic-analyzer signal walk, or a sensor check.",
        "- With 3-5 participants these are descriptive numbers, not statistical evidence. Report every session, "
        "including failures and withdrawals.",
        "",
    ]
    return "\n".join(out)
