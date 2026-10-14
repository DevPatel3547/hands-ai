"""Pluggable live-score sources + a bridge that turns score changes into
serial commands for the Pico ("SCORE <points> <new total>").

Why a host-side bridge instead of the Pico W polling the web itself: the
public scoreboard JSON is far larger than is comfortable to parse in the
Pico's ~200 KB of free RAM, and the bridge keeps Wi-Fi credentials off the
device. (Pico-side polling of a small relay endpoint is a reasonable next step.)

ESPN feed: uses the *unofficial, undocumented* endpoint
  https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
It is not a supported API: verify it still works, check ESPN's terms of use,
and keep the poll interval polite (>= 15 s) before relying on it.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


@dataclass(frozen=True)
class GameState:
    team: str
    team_score: int
    opponent: str
    opp_score: int
    status: str  # pre | in | post | none
    detail: str = ""


class ScoreFeed(Protocol):
    def poll(self) -> GameState: ...


class MockFeed:
    """Replays a scripted game, one state per poll (last state repeats)."""

    def __init__(self, states: Iterable[GameState]):
        self.states = list(states)
        self.i = 0

    def poll(self) -> GameState:
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return s


def demo_game(team: str = "PIT", opp: str = "CLE") -> MockFeed:
    seq = [
        (0, 0, "pre", "Sun 1:00 PM"),
        (0, 0, "in", "Q1 15:00"),
        (0, 3, "in", "Q1 6:12"),
        (6, 3, "in", "Q2 9:40"),
        (7, 3, "in", "Q2 9:40"),
        (10, 3, "in", "Q3 2:05"),
        (10, 3, "in", "Q4 4:44"),
        (17, 3, "in", "Q4 1:02"),
        (17, 3, "post", "Final"),
    ]
    return MockFeed(GameState(team, a, opp, b, st, d) for a, b, st, d in seq)


def parse_espn(data: dict, team: str = "PIT") -> GameState:
    """Extract one team's game from an ESPN scoreboard payload."""
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            cs = comp.get("competitors", [])
            mine = [c for c in cs if c.get("team", {}).get("abbreviation") == team]
            if not mine:
                continue
            other = [c for c in cs if c is not mine[0]]
            st = ev.get("status", {}).get("type", {})
            return GameState(
                team,
                int(mine[0].get("score") or 0),
                other[0].get("team", {}).get("abbreviation", "?") if other else "?",
                int(other[0].get("score") or 0) if other else 0,
                st.get("state", "none"),
                st.get("shortDetail", ""),
            )
    return GameState(team, 0, "", 0, "none", "no game on this scoreboard")


class EspnFeed:
    def __init__(self, team: str = "PIT", url: str = ESPN_SCOREBOARD, fetch: Callable[[str], bytes] | None = None):
        self.team, self.url = team, url
        self.fetch = fetch or (lambda u: urllib.request.urlopen(u, timeout=10).read())  # noqa: S310 - fixed https URL

    def poll(self) -> GameState:
        return parse_espn(json.loads(self.fetch(self.url)), self.team)


class ScoreWatcher:
    """Turns successive GameStates into scoring events for our team."""

    def __init__(self):
        self.last: int | None = None

    def update(self, gs: GameState) -> list[str]:
        cmds = []
        if gs.status in ("in", "post") and self.last is not None and gs.team_score > self.last:
            cmds.append(f"SCORE {gs.team_score - self.last} {gs.team_score}")
        if gs.status != "none":
            self.last = gs.team_score
        return cmds


def run_bridge(
    feed: ScoreFeed,
    send: Callable[[str], None],
    polls: int | None = None,
    interval_s: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> list[str]:
    w = ScoreWatcher()
    sent: list[str] = []
    i = 0
    while polls is None or i < polls:
        gs = feed.poll()
        cmds = w.update(gs)
        log(
            f"[bridge] {gs.team} {gs.team_score} - {gs.opp_score} {gs.opponent} ({gs.detail})"
            + (f" -> {', '.join(cmds)}" if cmds else "")
        )
        for c in cmds:
            send(c)
            sent.append(c)
        i += 1
        if gs.status == "post" and polls is None:
            break
        sleep(interval_s)
    return sent
