"""Regression tests for the v3 pipeline: enclosed-shape gates, exhaustive
enumeration, color labels, solution sets, quality gates and variant
emission — across all four base-parity combinations and both target colors.

The fake engine is adversarial: it derives the side to move from the move
list exactly like real KataGo, evaluates positions by WHO JUST PLAYED WHAT,
and its pv includes the candidate move itself."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generator"))

from board import Board, gtp_to_xy, opposite  # noqa: E402
from detect import (DeathEvent, validate_event, build_problem_set,  # noqa: E402
                    eyespace, move_is_connect_out, move_is_cut, bbox)

SIZE = 9


def cfg(**over):
    d = dict(margin=2, max_solutions=3, max_region_area=56, max_group=16,
             hard_max_region_area=140, hard_max_group=40,
             locality=0.15, min_quality=40, max_lines=24, allow_ko=False,
             allow_connect=False, settle_thresh=0.90, settled_check=0.70)
    d.update(over)
    return types.SimpleNamespace(**d)


class RuleKG:
    """Position-rule fake with a unique vital point VITAL:
    attacker on VITAL -> dead; defender on VITAL -> alive; once VITAL is
    occupied the verdict is frozen; any other appended move fails for its
    mover.  Ownership maps include stones like real output."""

    def __init__(self, target_color, target_xy, vital, base_len):
        self.tc = target_color
        self.target = target_xy
        self.vital = vital
        self.base_len = base_len
        self.queries = []

    def _grid(self, moves):
        g = {}
        for c, m in moves:
            p = gtp_to_xy(m, SIZE)
            if p:
                g[p] = c
        return g

    def _group_val(self, moves):
        g = self._grid(moves)
        v = g.get(gtp_to_xy(self.vital, SIZE))
        if v == opposite(self.tc):
            return -0.97
        if v == self.tc:
            return 0.97
        last = None
        for c, m in moves:
            if m.lower() != "pass":
                last = c
        n = len([m for m in moves if m[1].lower() != "pass"])
        if n > self.base_len:
            return 0.97 if last == opposite(self.tc) else -0.97
        return 0.55

    def query(self, moves, *, rules="chinese", komi=7.5, size=SIZE,
              max_visits=100, include_ownership=True,
              include_moves_ownership=False, **kw):
        self.queries.append([m[:] for m in moves])
        to_move = "W" if moves and moves[-1][0] == "B" else "B"
        val = self._group_val(moves)
        n = size * size
        own = [0.0] * n
        for (x, y), c in self._grid(moves).items():
            own[y * size + x] = 0.9 if c == "B" else -0.9
        for (x, y) in self.target:
            own[y * size + x] = val if self.tc == "B" else -val
        resp = {"id": "t", "turnNumber": len(moves),
                "rootInfo": {"currentPlayer": to_move},
                "ownership": own, "ownershipBlack": own[:],
                "moveInfos": []}
        if include_moves_ownership:
            win = own[:]
            alt = own[:]
            for (x, y) in self.target:
                s = 1 if self.tc == "B" else -1
                win[y * size + x] = (0.97 if to_move == self.tc else -0.97) * s
                alt[y * size + x] = (-0.97 if to_move == self.tc else 0.97) * s
            resp["moveInfos"] = [
                {"move": self.vital, "visits": 400, "winrate": 0.5,
                 "prior": 0.10,                       # hidden vital point
                 "pv": [self.vital, "H8"], "ownership": win,
                 "ownershipBlack": win[:]},
                {"move": "H8", "visits": 60, "winrate": 0.5, "prior": 0.4,
                 "pv": ["H8", "G8"], "ownership": alt,
                 "ownershipBlack": alt[:]},
            ]
        return resp


def corner_shape(target_color):
    """The classic enclosed straight three in the corner, colors swappable:
    target group A2 B2 C2 D2 D1, wall A3 B3 C3 D3 E1 E2; eyespace A1 B1 C1;
    vital point B1."""
    tc, atk = target_color, opposite(target_color)
    grp = ["A2", "B2", "C2", "D2", "D1"]
    wall = ["A3", "B3", "C3", "D3", "E1", "E2"]
    return tc, atk, grp, wall, "B1"


def make_case(target_color, base_to_move):
    tc, atk, grp, wall, vital = corner_shape(target_color)
    moves = []
    for i in range(max(len(grp), len(wall))):
        if i < len(grp):
            moves.append([tc, grp[i]])
        if i < len(wall):
            moves.append([atk, wall[i]])
    # keep strict alternation from an empty board: rebuild interleaved
    seq, gi, wi, turn = [], 0, 0, "B"
    fillers = {"B": iter(["G7", "H7", "G6", "H6", "G5", "H5"]),
               "W": iter(["G9", "H9", "F9", "F8", "G8", "H8"])}
    while gi < len(grp) or wi < len(wall):
        if turn == tc and gi < len(grp):
            seq.append([tc, grp[gi]]); gi += 1
        elif turn == atk and wi < len(wall):
            seq.append([atk, wall[wi]]); wi += 1
        else:
            seq.append([turn, next(fillers[turn])])
        turn = opposite(turn)
    nxt = "W" if seq[-1][0] == "B" else "B"
    if nxt != base_to_move:
        seq.append([nxt, next(fillers[nxt])])
    boards = [Board(SIZE)]
    bb = Board(SIZE)
    for c, m in seq:
        bb.play(c, m)
        boards.append(bb.copy())
    flip_move = next(fillers[base_to_move])
    moves_full = seq + [[base_to_move, flip_move]]
    b2 = bb.copy(); b2.play(base_to_move, flip_move)
    boards_full = boards + [b2.copy()]
    coords = frozenset(gtp_to_xy(m, SIZE) for m in grp)
    ev = DeathEvent(1, len(moves_full), tc, coords, 0.6, -0.98)
    return ev, boards_full, moves_full, vital


def run_case(target_color, base_to_move):
    tc, atk = target_color, opposite(target_color)
    ev, boards, moves, vital = make_case(tc, base_to_move)
    base_len = len([m for m in moves[:ev.flip_idx - 1]
                    if m[1].lower() != "pass"])
    kg = RuleKG(tc, sorted(ev.coords_now), vital, base_len)

    h, why = validate_event(kg, ev, boards, moves, size=SIZE,
                            rules="chinese", komi=7.5, visits=100,
                            thresh=0.90)
    assert h is not None, f"screen rejected: {why} ({tc=} {base_to_move=})"
    assert h.meta["eyespaceSize"] == 3 and h.meta["liberties"] == 3
    att_q, def_q = kg.queries[-2], kg.queries[-1]
    assert ("W" if att_q[-1][0] == "B" else "B") == atk
    assert ("W" if def_q[-1][0] == "B" else "B") == tc

    probs, why = build_problem_set(kg, h, size=SIZE, rules="chinese",
                                   komi=7.5, visits=100, prefix="t",
                                   cfg=cfg())
    assert not why, f"rejected: {why} ({tc=} {base_to_move=})"
    by = {p["status"]: p for p in probs}
    assert set(by) == {"undecided", "dead", "alive"}, set(by)

    u = by["undecided"]
    assert u["killing"]["moves"] == [vital]
    assert u["living"]["moves"] == [vital]
    assert u["meta"]["quality"] >= 80, (u["meta"]["quality"],
                                        u["meta"]["qualityComponents"])
    assert u["meta"]["solutionPrior"] == 0.10
    for l in u["killing"]["lines"]:
        for i, (c, m) in enumerate(l["seq"]):
            assert c == (atk if i % 2 == 0 else tc)
    for l in u["living"]["lines"]:
        for i, (c, m) in enumerate(l["seq"]):
            assert c == (tc if i % 2 == 0 else atk)
    for l in by["alive"]["explanationLines"]:
        assert l["result"] == "alive"
        for i, (c, m) in enumerate(l["seq"]):
            assert c == (atk if i % 2 == 0 else tc)
    for l in by["dead"]["explanationLines"]:
        assert l["result"] == "dead"
        for i, (c, m) in enumerate(l["seq"]):
            assert c == (tc if i % 2 == 0 else atk)
    return True


def test_gate_open_group():
    """The same group WITHOUT its wall must be rejected as open."""
    tc = "B"
    ev, boards, moves, vital = make_case(tc, "W")
    # strip the wall: rebuild boards without white stones near the corner
    b = Board(SIZE)
    for m in ["A2", "B2", "C2", "D2", "D1"]:
        b.play("B", m)
    space, is_open = eyespace(b, [gtp_to_xy(m, SIZE) for m in
                                  ["A2", "B2", "C2", "D2", "D1"]], 12)
    assert is_open, "wall-less group must be open"


def test_gate_connect_and_cut():
    """A living move that links out of the region, and a killing move that
    severs an outside support group, must be detected."""
    b = Board(SIZE)
    grp = ["A2", "B2", "C2"]
    for m in grp:
        b.play("B", m)
    # outside support column reaching far from the region
    for m in ["E2", "E3", "E4", "E5", "E6", "E7"]:
        b.play("B", m)
    for m in ["A3", "B3", "C3", "D3", "D1"]:
        b.play("W", m)
    target = [gtp_to_xy(m, SIZE) for m in grp]
    region = bbox(target, SIZE, 1)
    assert move_is_connect_out(b, "D2", target, "B", region)
    assert move_is_cut(b, "D2", target, "B", region)
    assert not move_is_connect_out(b, "B1", target, "B", region)
    assert not move_is_cut(b, "B1", target, "B", region)


def test_gate_obviousness():
    """A solution the raw policy already names must lose quality."""
    tc = "B"
    ev, boards, moves, vital = make_case(tc, "W")
    base_len = len([m for m in moves[:ev.flip_idx - 1]
                    if m[1].lower() != "pass"])

    class ObviousKG(RuleKG):
        def query(self, ms, **kw):
            r = super().query(ms, **kw)
            for mi in r["moveInfos"]:
                if mi["move"] == self.vital:
                    mi["prior"] = 0.92        # first-glance answer
            return r

    kg = ObviousKG(tc, sorted(ev.coords_now), vital, base_len)
    h, why = validate_event(kg, ev, boards, moves, size=SIZE,
                            rules="chinese", komi=7.5, visits=100,
                            thresh=0.90)
    assert h is not None, why
    probs, why = build_problem_set(kg, h, size=SIZE, rules="chinese",
                                   komi=7.5, visits=100, prefix="t",
                                   cfg=cfg())
    assert probs, why
    q = probs[0]["meta"]["quality"]
    assert q <= 78, f"obvious problem scored {q}"
    assert probs[0]["meta"]["qualityComponents"]["obviousness"] < -15


if __name__ == "__main__":
    for tc in ("B", "W"):
        for btm in ("B", "W"):
            run_case(tc, btm)
            print(f"OK  target={tc}  base_to_move={btm}")
    test_gate_open_group()
    print("OK  gate: open group rejected")
    test_gate_connect_and_cut()
    print("OK  gate: connect-out / cut detected")
    test_gate_obviousness()
    print("OK  gate: obvious solutions penalized")
    print("all v3 pipeline cases passed")
