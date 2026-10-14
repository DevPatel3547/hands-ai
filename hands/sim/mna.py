"""Tiny DC nodal-analysis solver with piecewise-linear diodes and NPN switches.

Good enough to answer the questions a bench needs: is this LED lit, how much
current flows through the INA219 shunt, does this probe point read high or
low, is a GPIO being overloaded. Not a SPICE replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

G_LEAK = 1e-8  # every node leaks to ground (10^8 ohm) so floating nodes solve to 0 V
G_OFF = 1e-9


@dataclass
class Source:  # Thevenin source to ground: V behind R
    node: str
    v: float
    r: float
    tag: str = ""


@dataclass
class Res:
    a: str
    b: str
    r: float
    tag: str = ""


@dataclass
class Diode:
    a: str  # anode
    k: str  # cathode
    vf: float
    rd: float
    tag: str = ""
    on: bool = False


@dataclass
class NPN:
    b: str
    c: str
    e: str
    tag: str = ""
    vbe: float = 0.7
    rbe: float = 100.0
    rce_sat: float = 2.0
    ib_min: float = 5e-5
    sat: bool = False
    be_on: bool = False


@dataclass
class Circuit:
    ground: str
    sources: list[Source] = field(default_factory=list)
    res: list[Res] = field(default_factory=list)
    diodes: list[Diode] = field(default_factory=list)
    npns: list[NPN] = field(default_factory=list)


@dataclass
class Solution:
    v: dict[str, float]
    circuit: Circuit

    def volt(self, node: str | None) -> float:
        if node is None:
            return 0.0
        return self.v.get(node, 0.0)

    def source_current(self, tag: str) -> float:
        """Current (A) flowing OUT of the source with ``tag``."""
        tot = 0.0
        for s in self.circuit.sources:
            if s.tag == tag:
                tot += (s.v - self.volt(s.node)) / s.r
        return tot

    def res_current(self, tag: str) -> float:
        for r in self.circuit.res:
            if r.tag == tag:
                return (self.volt(r.a) - self.volt(r.b)) / r.r
        return 0.0

    def diode_current(self, tag: str) -> float:
        for d in self.circuit.diodes:
            if d.tag == tag:
                if not d.on:
                    return 0.0
                return max(0.0, (self.volt(d.a) - self.volt(d.k) - d.vf) / d.rd)
        return 0.0

    def npn_ic(self, tag: str) -> float:
        for q in self.circuit.npns:
            if q.tag == tag and q.sat:
                return (self.volt(q.c) - self.volt(q.e)) / q.rce_sat
        return 0.0


def _solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-18:
            continue
        M[col], M[piv] = M[piv], M[col]
        p = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / p
            if f:
                row_r, row_c = M[r], M[col]
                for c in range(col, n + 1):
                    row_r[c] -= f * row_c[c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / M[r][r] if abs(M[r][r]) > 1e-18 else 0.0
    return x


def solve(ckt: Circuit, max_iter: int = 40) -> Solution:
    nodes: set[str] = set()
    for s in ckt.sources:
        nodes.add(s.node)
    for r in ckt.res:
        nodes |= {r.a, r.b}
    for d in ckt.diodes:
        nodes |= {d.a, d.k}
    for q in ckt.npns:
        nodes |= {q.b, q.c, q.e}
    nodes.discard(ckt.ground)
    idx = {n: i for i, n in enumerate(sorted(nodes))}
    N = len(idx)
    v: dict[str, float] = {ckt.ground: 0.0}
    for d in ckt.diodes:
        d.on = False
    for q in ckt.npns:
        q.sat = q.be_on = False

    for _ in range(max_iter):
        A = [[0.0] * N for _ in range(N)]
        b = [0.0] * N

        def g(a, bb, gg):
            ia, ib = idx.get(a), idx.get(bb)
            if ia is not None:
                A[ia][ia] += gg
            if ib is not None:
                A[ib][ib] += gg
            if ia is not None and ib is not None:
                A[ia][ib] -= gg
                A[ib][ia] -= gg

        def inj(n, i):  # current i injected INTO node n
            k = idx.get(n)
            if k is not None:
                b[k] += i

        for _n, k in idx.items():
            A[k][k] += G_LEAK
        for s in ckt.sources:
            g(s.node, ckt.ground, 1 / s.r)
            inj(s.node, s.v / s.r)
        for r in ckt.res:
            g(r.a, r.b, 1 / r.r)
        for d in ckt.diodes:
            if d.on:
                g(d.a, d.k, 1 / d.rd)
                inj(d.a, d.vf / d.rd)
                inj(d.k, -d.vf / d.rd)
            else:
                g(d.a, d.k, G_OFF)
        for q in ckt.npns:
            if q.be_on:
                g(q.b, q.e, 1 / q.rbe)
                inj(q.b, q.vbe / q.rbe)
                inj(q.e, -q.vbe / q.rbe)
            else:
                g(q.b, q.e, G_OFF)
            g(q.c, q.e, 1 / q.rce_sat if q.sat else G_OFF)
        x = _solve_linear(A, b) if N else []
        v = {n: x[i] for n, i in idx.items()}
        v[ckt.ground] = 0.0
        changed = False
        for d in ckt.diodes:
            vd = v.get(d.a, 0) - v.get(d.k, 0)
            new = (vd - d.vf) / d.rd > 1e-6 if d.on else vd > d.vf + 1e-6
            if new != d.on:
                d.on, changed = new, True
        for q in ckt.npns:
            vbe = v.get(q.b, 0) - v.get(q.e, 0)
            new_be = (vbe - q.vbe) / q.rbe > 1e-7 if q.be_on else vbe > q.vbe + 1e-6
            ib = (vbe - q.vbe) / q.rbe if new_be else 0.0
            new_sat = new_be and ib > q.ib_min
            if (new_be, new_sat) != (q.be_on, q.sat):
                q.be_on, q.sat, changed = new_be, new_sat, True
        if not changed:
            break
    return Solution(v, ckt)
