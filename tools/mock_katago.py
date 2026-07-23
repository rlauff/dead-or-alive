#!/usr/bin/env python3
"""A tiny fake `katago analysis` engine for testing the pipeline offline.

It replays one scripted 9x9 game in which Black builds the straight-three
corner group (A2 B2 C2 D2 D1) and White kills it with B1 at move 14, and it
answers analysis queries with hand-modelled ownership maps:

    group contested (nobody on B1)  ->  +0.60 for Black on the group
    White stone on B1               ->  -0.98 (dead)
    Black stone on B1               ->  +0.98 (alive)

Ownership is reported from BLACK's perspective (i.e. it emulates
reportAnalysisWinratesAs = BLACK).  Good enough to exercise self-play,
flip detection, validation, settled checks and problem emission:

    python3 generator/generate.py --katago tools/mock_katago.py \
        --model fake --config fake --size 9 --games 1

Not a Go engine.  Do not use for anything but testing.
"""
import json
import sys

GTP_COLS = "ABCDEFGHJKLMNOPQRST"

SCRIPT = [
    ["B", "A2"], ["W", "A3"], ["B", "B2"], ["W", "B3"], ["B", "C2"],
    ["W", "C3"], ["B", "D2"], ["W", "D3"], ["B", "D1"], ["W", "E2"],
    ["B", "G7"], ["W", "E1"], ["B", "F5"], ["W", "B1"], ["B", "G3"],
    ["W", "pass"], ["B", "pass"],
]

GROUP = ["A2", "B2", "C2", "D2", "D1"]
EYESPACE = ["A1", "B1", "C1"]


def xy(move, size):
    m = move.strip().upper()
    if m == "PASS":
        return None
    return GTP_COLS.index(m[0]), size - int(m[1:])


def build(q):
    size = q["boardXSize"]
    grid = {}
    for c, mv in q.get("initialStones", []):
        p = xy(mv, size)
        if p:
            grid[p] = c
    for c, mv in q.get("moves", []):
        p = xy(mv, size)
        if p:
            grid[p] = c            # no captures in the script; fine
    if q.get("moves"):
        to_move = "W" if q["moves"][-1][0] == "B" else "B"
    else:
        to_move = q.get("initialPlayer", "B")
    return size, grid, to_move


def on_script(q):
    ms = q.get("moves", [])
    return ms == SCRIPT[:len(ms)]


def ownership(size, grid, b1_override=None, last_mover=None, off=False):
    """Black-perspective map: stones +/-0.9, tsumego region by B1 status.
    Off-script positions (enumeration tries) are decisive: with B1 empty
    the group's fate follows the last mover — a failed attacker try leaves
    it alive, a failed defender try leaves it dead."""
    own = [0.0] * (size * size)
    for (x, y), c in grid.items():
        own[y * size + x] = 0.9 if c == "B" else -0.9
    if all(grid.get(xy(m, size)) == "B" for m in GROUP):
        b1 = b1_override if b1_override is not None \
            else grid.get(xy("B1", size))
        if b1 == "W":
            val = -0.98
        elif b1 == "B":
            val = 0.98
        elif off and last_mover == "W":
            val = 0.98      # attacker tried elsewhere: group lives
        elif off and last_mover == "B":
            val = -0.98     # defender tried elsewhere: group dies
        else:
            val = 0.60
        for m in GROUP + EYESPACE:
            x, y = xy(m, size)
            own[y * size + x] = val
    return own


def mi(move, visits, own=None, pv=None):
    d = {"move": move, "visits": visits, "winrate": 0.5, "order": 0,
         "pv": pv or [move], "prior": 0.5, "scoreLead": 0.0}
    if own is not None:
        d["ownership"] = own
    return d


def answer(q):
    size, grid, to_move = build(q)
    ms = q.get("moves", [])
    last_mover = None
    for c, m in ms:
        if m.lower() != "pass":
            last_mover = c
    off = not on_script(q)
    resp = {"id": q["id"], "turnNumber": len(q.get("moves", [])),
            "isDuringSearch": False,
            "rootInfo": {"currentPlayer": to_move, "winrate": 0.5,
                         "visits": q.get("maxVisits", 100),
                         "scoreLead": 0.0}}
    if q.get("includeOwnership"):
        resp["ownership"] = ownership(size, grid, last_mover=last_mover,
                                      off=off)

    group_present = size == 9 and \
        all(grid.get(xy(m, size)) == "B" for m in GROUP)
    b1 = grid.get(xy("B1", 9)) if size == 9 else None

    if q.get("includeMovesOwnership") and group_present:
        if b1 == "W":                       # settled dead; defender tries
            infos = [mi("A1", 300, ownership(size, grid, "W"), ["A1", "C1"]),
                     mi("C1", 250, ownership(size, grid, "W"), ["C1", "A1"])]
        elif b1 == "B":                     # settled alive; attacker tries
            infos = [mi("J9", 300, ownership(size, grid, "B"), ["J9"]),
                     mi("A5", 220, ownership(size, grid, "B"), ["A5"])]
        elif to_move == "W":                # hinge, attacker to move
            infos = [mi("B1", 400, ownership(size, grid, "W"),
                        ["B1", "A1", "C1"]),
                     mi("J9", 100, ownership(size, grid, None), ["J9", "B1"])]
        else:                               # hinge, defender to move
            infos = [mi("B1", 380, ownership(size, grid, "B"), ["B1"]),
                     mi("J9", 90, ownership(size, grid, "W"), ["J9", "B1"])]
        resp["moveInfos"] = infos
        return resp

    # self-play: follow the script
    n = len(q.get("moves", []))
    if size == 9 and n < len(SCRIPT) and q.get("moves", []) == SCRIPT[:n]:
        resp["moveInfos"] = [mi(SCRIPT[n][1], q.get("maxVisits", 48))]
    else:
        resp["moveInfos"] = [mi("pass", q.get("maxVisits", 48))]
    return resp


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        q = json.loads(line)
        if q.get("action"):
            print(json.dumps(q), flush=True)
            continue
        print(json.dumps(answer(q)), flush=True)


if __name__ == "__main__":
    main()
