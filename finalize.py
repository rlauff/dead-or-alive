#!/usr/bin/env python3
"""High-effort finalizer for ACCEPTED tsumego.

Walk every problem in accepted/ (or --dir) and, at very high visits
(default 5000), turn it into a polished, deeply-verified problem:

  * RE-VERIFY the claimed status from scratch.  Undecided must still have a
    killing move (attacker first) AND a living move (defender first); dead
    must stay dead against every local defence; alive must stay alive
    against every local attack.  A problem that fails is DELETED — the whole
    point is that whatever survives is trustworthy.

  * RECOMPUTE the marked group.  The target stones are re-derived from the
    actual board (the connected group at the recorded target points), so an
    over- or under-marked group is fixed.  If the recorded target points no
    longer share one colour / one group, the problem is deleted.

  * UNCROP.  The region hint is removed so the whole board is shown (13x13
    and smaller are perfectly readable; a cramped crop hides ladders,
    approach moves and outside liberties that matter).

  * DROP the last move.  Only the group under investigation is marked; no
    "last move played" dot, which would give the answer away or mislead.

  * DEEP, WIDE solution lines.  Every solution move keeps a deep principal
    variation (as long as the fight stays local).  In addition a WEAK
    SOLUTION is attached: for every half-way sensible first move the
    challenger might try, a single refuting reply is stored, so clicking any
    plausible wrong move in the UI shows why it fails.  This is what
    "precomputed deeply enough" means — the solution set is confirmed by
    exhaustion, not by trusting one line.

Problems can grow to ~1 MB; that is fine.  Files are rewritten in place;
web/problems.json is rebuilt at the end.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "generator"))

from board import Board, gtp_to_xy, xy_to_gtp, opposite  # noqa: E402
from katago_client import KataGo, wait_ready  # noqa: E402
from detect import (bbox, group_mean_own, local_empty_points,  # noqa: E402
                    line_has_ko, trim_line_to_region, _pv_to_line)


# --------------------------------------------------------------------- utils
def board_from_stones(stones, size) -> Board:
    b = Board(size)
    for c, mv in stones:
        xy = gtp_to_xy(mv, size)
        if xy:
            b.grid[xy[1]][xy[0]] = c
    return b


def recompute_target(board: Board, recorded_pts, color):
    """Re-derive the exact group from the recorded target points.  Returns
    (sorted_coords, reason_or_None).  Fails if the points are not all the
    same colour or do not all belong to a single connected group."""
    xs = [gtp_to_xy(m, board.size) for m in recorded_pts]
    xs = [p for p in xs if p is not None]
    on_color = [p for p in xs if board.get(*p) == color]
    if not on_color:
        return None, "no recorded target stone is on the board any more"
    groups = []
    seen = set()
    for p in on_color:
        if p in seen:
            continue
        st, _ = board.group_at(*p)
        seen |= st
        groups.append(st)
    if len(groups) > 1:
        # recorded points span several groups — the mark was wrong/ambiguous
        # keep the group containing the MOST recorded points
        groups.sort(key=lambda g: -len(set(g) & set(on_color)))
    target = sorted(groups[0])
    return target, None


def _first_of(prefix_moves, fallback):
    """initialPlayer for a query: whoever plays the first move of the
    sequence (the base is a static position, so there is no history)."""
    return prefix_moves[0][0] if prefix_moves else fallback


def result_of(score):
    return "alive" if score > 0.5 else ("dead" if score < -0.5 else "unclear")


# ------------------------------------------------------------- publishing
WEB = ROOT / "web"
PUB = WEB / "problems"
MANIFEST = WEB / "manifest.json"


def manifest_for(dirname: str) -> Path:
    """One manifest per source directory, so reviewed and unreviewed pools
    stay separate: accepted -> manifest.json (what the player loads by
    default), candidates -> manifest-candidates.json (loadable with
    ?pool=candidates), anything else -> manifest-<name>.json."""
    return WEB / ("manifest.json" if dirname == "accepted"
                  else f"manifest-{dirname}.json")


def load_manifest(path: Path = MANIFEST) -> list[str]:
    """Existing manifest entries, so re-running on a subset doesn't drop
    problems finalized earlier."""
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(d, list):
        return [str(x) for x in d]
    return [str(x) for x in d.get("problems", [])]


def save_manifest(names, path: Path = MANIFEST) -> int:
    """Write the filename list the web app loads.  The player cannot list a
    directory on a static/restricted server, so this file IS the index."""
    WEB.mkdir(exist_ok=True)
    uniq = sorted(set(names))
    path.write_text(json.dumps(
        {"problems": uniq, "count": len(uniq)}, indent=1))
    return len(uniq)


def publish(problem: dict, filename: str) -> None:
    """Copy a finalized problem into web/problems/ where the player fetches
    it by name."""
    PUB.mkdir(parents=True, exist_ok=True)
    (PUB / filename).write_text(json.dumps(problem, indent=1))


def unpublish(filename: str) -> None:
    f = PUB / filename
    if f.exists():
        f.unlink()


def follow_pv(kg, stones, prefix_moves, first, mv, target, color, *, size,
              rules, komi, visits, region, max_plies):
    """Greedy deep readout: from the position after `first mv`, repeatedly
    play the engine's top move, extending the line ply by ply until it
    leaves the region, passes, or hits `max_plies`.  Returns (score, seq)."""
    seq = [[first, mv]]
    line_moves = list(prefix_moves) + [[first, mv]]
    to_move = opposite(first)
    last_score = None
    for _ in range(max_plies):
        try:
            resp = kg.query(line_moves, initial_stones=stones,
                            initial_player=first, rules=rules, komi=komi,
                            size=size, max_visits=visits,
                            include_ownership=True)
        except RuntimeError as e:
            if "llegal" in str(e):
                break
            raise
        last_score = group_mean_own(target, resp["ownershipBlack"], size, color)
        best = None
        for mi in sorted(resp.get("moveInfos", []),
                         key=lambda m: -m.get("visits", 0)):
            best = mi["move"]
            break
        if not best or best.lower() == "pass":
            break
        xy = gtp_to_xy(best, size)
        if xy is None:
            break
        # stop once the line wanders outside the (enlarged) region
        if not (region["x0"] - 1 <= xy[0] <= region["x1"] + 1
                and region["y0"] - 1 <= xy[1] <= region["y1"] + 1):
            break
        seq.append([to_move, best])
        line_moves.append([to_move, best])
        to_move = opposite(to_move)
    if last_score is None:
        # evaluate the position right after the first move
        resp = kg.query(list(prefix_moves) + [[first, mv]],
                        initial_stones=stones, initial_player=first,
                        rules=rules, komi=komi, size=size, max_visits=visits,
                        include_ownership=True)
        last_score = group_mean_own(target, resp["ownershipBlack"], size, color)
    return last_score, seq


def widen(region, coords, size, pad=1):
    """Grow a region so it covers `coords` (used when a candidate move lies
    outside the group's neighbourhood, so its refutation is not trimmed)."""
    r = dict(region)
    for (x, y) in coords:
        r["x0"] = min(r["x0"], max(0, x - pad))
        r["y0"] = min(r["y0"], max(0, y - pad))
        r["x1"] = max(r["x1"], min(size - 1, x + pad))
        r["y1"] = max(r["y1"], min(size - 1, y + pad))
    return r


def node_candidates(kg, stones, prefix, mover, bd, target, *, size, rules,
                    komi, visits, margin, top_n):
    """Moves to try for `mover`: every empty point near the group PLUS the
    moves the engine itself rates highest.

    Distance alone is a bad filter — a vital point can sit well outside the
    group's neighbourhood (a distant placement that reduces the eyespace, a
    ladder breaker, an approach that removes a liberty). Those moves are
    exactly the ones a strong net puts its visits on, so taking its top
    choices guarantees they are explored no matter how far away they are."""
    pts = list(local_empty_points(bd, target, size, margin))
    if top_n <= 0:
        return pts
    seen = set(pts)
    try:
        resp = kg.query(prefix, initial_stones=stones,
                        initial_player=_first_of(prefix, mover),
                        rules=rules, komi=komi, size=size,
                        max_visits=visits, include_ownership=False)
    except RuntimeError:
        return pts
    for mi in sorted(resp.get("moveInfos", []),
                     key=lambda m: -m.get("visits", 0))[:top_n]:
        mv = mi["move"]
        if mv.lower() == "pass" or mv in seen:
            continue
        xy = gtp_to_xy(mv, size)
        if xy and bd.get(*xy) == Board.EMPTY:
            seen.add(mv)
            pts.append(mv)
    return pts


def batch_tries(kg, stones, prefix_moves, challenger, moves, defender,
                target, color, *, size, rules, komi, visits, region):
    """Try MANY challenger moves at once.

    Two pipelined rounds: first every candidate move, then (for those that
    got a reply) the position after the defender's refutation.  Each round
    goes to KataGo as one batch, so the engine can interleave the searches
    and keep the NN batch full instead of idling between single queries.

    Returns a list aligned with `moves` of (score_after, reply) — or
    (None, None) for a move the engine rejected."""
    ip = _first_of(prefix_moves, challenger)
    common = dict(initial_stones=stones, initial_player=ip, rules=rules,
                  komi=komi, size=size, max_visits=visits,
                  include_ownership=True)
    r1 = kg.query_many([dict(moves=prefix_moves + [[challenger, mv]],
                             include_moves_ownership=True, **common)
                        for mv in moves])
    replies, out = [], []
    for mv, resp in zip(moves, r1):
        if isinstance(resp, Exception):
            replies.append(None)
            out.append((None, None))
            continue
        sc = group_mean_own(target, resp["ownershipBlack"], size, color)
        rep = None
        for mi in sorted(resp.get("moveInfos", []),
                         key=lambda m: -m.get("visits", 0)):
            if mi["move"].lower() == "pass":
                continue
            rx = gtp_to_xy(mi["move"], size)
            if rx and (region["x0"] - 1 <= rx[0] <= region["x1"] + 1
                       and region["y0"] - 1 <= rx[1] <= region["y1"] + 1):
                rep = mi["move"]
            break
        replies.append(rep)
        out.append((round(sc, 3), rep))
    # round two: score after each refutation
    idx = [i for i, r in enumerate(replies) if r is not None]
    if idx:
        r2 = kg.query_many([
            dict(moves=prefix_moves + [[challenger, moves[i]],
                                       [defender, replies[i]]], **common)
            for i in idx])
        for i, resp in zip(idx, r2):
            if isinstance(resp, Exception):
                continue
            sc = group_mean_own(target, resp["ownershipBlack"], size, color)
            out[i] = (round(sc, 3), replies[i])
    return out


def try_and_refute(kg, stones, prefix_moves, challenger, mv, defender,
                   target, color, *, size, rules, komi, visits, region):
    """Play `challenger mv` and find the defender's single best reply.
    Returns (score_after_reply, reply_or_None, score_after_try)."""
    try:
        resp = kg.query(prefix_moves + [[challenger, mv]],
                        initial_stones=stones, initial_player=_first_of(
                            prefix_moves, challenger),
                        rules=rules, komi=komi, size=size, max_visits=visits,
                        include_ownership=True, include_moves_ownership=True)
    except RuntimeError as e:
        if "llegal" in str(e):
            return None, None, None
        raise
    score_try = group_mean_own(target, resp["ownershipBlack"], size, color)
    reply = None
    for mi in sorted(resp.get("moveInfos", []),
                     key=lambda m: -m.get("visits", 0)):
        if mi["move"].lower() == "pass":
            continue
        rx = gtp_to_xy(mi["move"], size)
        if rx and (region["x0"] - 1 <= rx[0] <= region["x1"] + 1
                   and region["y0"] - 1 <= rx[1] <= region["y1"] + 1):
            reply = mi["move"]
        break
    if reply is None:
        return round(score_try, 3), None, round(score_try, 3)
    # score after the refutation is what the challenger has to accept
    try:
        r2 = kg.query(prefix_moves + [[challenger, mv], [defender, reply]],
                      initial_stones=stones,
                      initial_player=_first_of(prefix_moves, challenger),
                      rules=rules, komi=komi, size=size, max_visits=visits,
                      include_ownership=True)
        score_after = group_mean_own(target, r2["ownershipBlack"], size, color)
    except RuntimeError:
        score_after = score_try
    return round(score_after, 3), reply, round(score_try, 3)


def weak_dialogue(kg, stones, base_moves, challenger, board, target, color,
                  *, want, size, rules, komi, visits, margin, region, depth,
                  branch, log, top_n=5, _path=None, _prefix=None, _bd=None):
    """The challenger keeps trying until convinced.

    At each node every local empty point is tried; the defender answers each
    with its single best refuting move.  The most tempting failures (the
    ones that come closest to working) are then continued: the challenger
    plays on from there, gets refuted again, and so on for `depth`
    challenger moves.  Returns FLATTENED lines — full sequences from the
    root — which is exactly what the board's click-through book consumes.

    `want` is what the challenger is trying to achieve ("dead" for an
    attacker, "alive" for a defender), used to rank temptingness."""
    defender = opposite(challenger)
    path = _path or []
    prefix = _prefix if _prefix is not None else list(base_moves)
    bd = _bd if _bd is not None else board
    live_target = [t for t in target if bd.get(*t) == color]
    if len(live_target) < 1:
        return []
    lines, nodes = [], []
    cands = node_candidates(kg, stones, prefix, challenger, bd, live_target,
                            size=size, rules=rules, komi=komi, visits=visits,
                            margin=margin, top_n=top_n)
    reg = widen(region, [c for c in (gtp_to_xy(m, size) for m in cands) if c],
                size)
    res = batch_tries(kg, stones, prefix, challenger, cands, defender,
                      live_target, color, size=size, rules=rules, komi=komi,
                      visits=visits, region=reg)
    for mv, (sc_after, reply) in zip(cands, res):
        if sc_after is None:
            continue
        seg = [[challenger, mv]] + ([[defender, reply]] if reply else [])
        full = path + seg
        lines.append({"seq": full, "groupScore": sc_after,
                      "result": result_of(sc_after),
                      "ko": line_has_ko(board, full)})
        nodes.append({"mv": mv, "reply": reply, "score": sc_after, "seg": seg})
        if log:
            log(f"    {'  ' * len(path)}{challenger} {mv:>4}"
                f"{' -> ' + reply if reply else ''}: {sc_after:+.2f}")
    if depth <= 1:
        return lines
    # continue the most tempting failures one challenger move deeper
    rank = (lambda n: n["score"]) if want == "dead" else (lambda n: -n["score"])
    for n in sorted(nodes, key=rank)[:branch]:
        if not n["reply"]:
            continue
        nb = bd.copy()
        try:
            for c, m in n["seg"]:
                nb.play(c, m)
        except Exception:                     # noqa: BLE001 illegal in replay
            continue
        lines.extend(weak_dialogue(
            kg, stones, base_moves, challenger, board, target, color,
            want=want,
            size=size, rules=rules, komi=komi, visits=visits, margin=margin,
            region=region, depth=depth - 1, branch=branch, log=log,
            top_n=top_n,
            _path=path + n["seg"], _prefix=prefix + n["seg"], _bd=nb))
    return lines


def enumerate_side(kg, stones, base_moves, mover, board, target, color, *,
                   size, rules, komi, visits, margin, region, log, top_n=5):
    """Try every local empty point as `mover`'s first move.  The base is a
    static position, so `mover` is made the side to move by inserting a
    single pass for the opponent (never two passes).  For each try, compute
    the group score and a SINGLE-reply refutation line.  Returns a list of
    {move, score, seq, ko}."""
    defender = opposite(mover)
    # static position: initialPlayer makes `mover` the side to move, so no
    # pass is inserted (a pass would alter the analysis and, paired with
    # another, would look like the game ending)
    prefix = list(base_moves)
    cands = node_candidates(kg, stones, prefix, mover, board, target,
                            size=size, rules=rules, komi=komi, visits=visits,
                            margin=margin, top_n=top_n)
    reg = widen(region, [c for c in (gtp_to_xy(m, size) for m in cands) if c],
                size)
    res = batch_tries(kg, stones, prefix, mover, cands, defender, target,
                      color, size=size, rules=rules, komi=komi,
                      visits=visits, region=reg)
    out = []
    for mv, (sc, reply) in zip(cands, res):
        if sc is None:
            continue
        seq = [[mover, mv]] + ([[defender, reply]] if reply else [])
        d = {"move": mv, "score": sc, "seq": seq, "ko": line_has_ko(board, seq)}
        out.append(d)
        if log:
            log(f"    {mover} {mv:>4}: group {sc:+.2f}")
    return out


# ----------------------------------------------------------------- per-file
def finalize(kg, path: Path, args, log):
    """Return (problem, "") or (None, reason) if it must be deleted.
    `log` receives only the fine-grained per-move detail (verbose mode)."""
    p = json.loads(path.read_text())
    size = p["boardSize"]
    rules, komi = p["rules"], p["komi"]
    color = p["targetColor"]
    attacker = opposite(color)
    status = p["status"]
    V = args.visits
    board = board_from_stones(p["initialStones"], size)

    # ---- recompute the marked group ------------------------------------
    target, why = recompute_target(board, p["targetStones"], color)
    if target is None:
        return None, why
    if len(target) < 3:
        return None, f"group is only {len(target)} stone(s) after recompute"
    p["targetStones"] = [xy_to_gtp(x, y, size) for (x, y) in target]
    region = bbox(target, size, args.margin)

    # who is to move at the base position (from the stored move parity is
    # unavailable here — the base is a static position, so we insert a pass
    # for whichever side we are analysing).  We treat the base as "either to
    # move": attacker-to-move for the kill, defender-to-move for the live.
    base_moves: list[list[str]] = []       # static position, no move history
    stones = p["initialStones"]            # THE position — every query needs it

    # ---- exhaustive re-verification at high visits ---------------------
    if status == "undecided":
        kills = enumerate_side(kg, stones, base_moves, attacker, board, target, color,
                               size=size, rules=rules, komi=komi, visits=V,
                               margin=args.margin, region=region, log=log,
                               top_n=args.policy_top)
        lives = enumerate_side(kg, stones, base_moves, color, board, target, color,
                               size=size, rules=rules, komi=komi, visits=V,
                               margin=args.margin, region=region, log=log,
                               top_n=args.policy_top)
        killing = [d for d in kills if d["score"] <= -args.thresh]
        living = [d for d in lives if d["score"] >= args.thresh]
        if not killing:
            return None, "no killing move confirmed at high visits"
        if not living:
            return None, "no living move confirmed at high visits"
        # deep main lines for the confirmed solution moves
        p["killing"] = _solution_block(kg, stones, base_moves, attacker, color, board,
                                       target, killing, color, args, region,
                                       size, rules, komi, log)
        p["living"] = _solution_block(kg, stones, base_moves, color, attacker, board,
                                      target, living, attacker, args, region,
                                      size, rules, komi, log)
        # weak solution: every non-solution try, one-reply refutation
        # weak solution: the challenger keeps trying, several moves deep,
        # until convinced.  Both sides get their own dialogue.
        wv = args.weak_visits or args.visits
        kill_weak = weak_dialogue(
            kg, stones, base_moves, attacker, board, target, color, want="dead",
            size=size, rules=rules, komi=komi, visits=wv, margin=args.margin,
            region=region, depth=args.weak_depth, branch=args.weak_branch,
            log=log, top_n=args.policy_top)
        live_weak = weak_dialogue(
            kg, stones, base_moves, color, board, target, color, want="alive",
            size=size, rules=rules, komi=komi, visits=wv, margin=args.margin,
            region=region, depth=args.weak_depth, branch=args.weak_branch,
            log=log, top_n=args.policy_top)
        p["weakSolution"] = {"killTries": kill_weak, "liveTries": live_weak}
        p["explanationLines"] = _cap(kill_weak + live_weak, args.max_lines)

    elif status == "dead":
        # the group is claimed dead: every local defender move must fail
        rescue = enumerate_side(kg, stones, base_moves, color, board, target, color,
                                size=size, rules=rules, komi=komi, visits=V,
                                margin=args.margin, region=region, log=log,
                               top_n=args.policy_top)
        saved = [d for d in rescue if d["score"] > -args.settled_check]
        if saved:
            return None, (f"{saved[0]['move']} rescues the group "
                          f"({saved[0]['score']:+.2f}) — not actually dead")
        wv = args.weak_visits or args.visits
        weak = weak_dialogue(
            kg, stones, base_moves, color, board, target, color, want="alive",
            size=size, rules=rules, komi=komi, visits=wv, margin=args.margin,
            region=region, depth=args.weak_depth, branch=args.weak_branch,
            log=log, top_n=args.policy_top)
        p["weakSolution"] = {"defenceTries": weak}
        p["explanationLines"] = _cap(weak, args.max_lines)

    elif status == "alive":
        benson = board.is_pass_alive(target, color)
        attack = enumerate_side(kg, stones, base_moves, attacker, board, target, color,
                                size=size, rules=rules, komi=komi, visits=V,
                                margin=args.margin, region=region, log=log,
                               top_n=args.policy_top)
        killed = [d for d in attack if d["score"] < args.settled_check]
        if killed and not benson:
            return None, (f"{killed[0]['move']} kills the group "
                          f"({killed[0]['score']:+.2f}) — not actually alive")
        p["meta"]["bensonAlive"] = benson
        wv = args.weak_visits or args.visits
        weak = weak_dialogue(
            kg, stones, base_moves, attacker, board, target, color, want="dead",
            size=size, rules=rules, komi=komi, visits=wv, margin=args.margin,
            region=region, depth=args.weak_depth, branch=args.weak_branch,
            log=log, top_n=args.policy_top)
        p["weakSolution"] = {"attackTries": weak}
        p["explanationLines"] = _cap(weak, args.max_lines)
    else:
        return None, f"unknown status {status!r}"

    # ---- uncrop + drop last move ---------------------------------------
    p.pop("region", None)                  # frontend shows full board
    p.pop("lastMove", None)                # only the group is marked
    p["meta"]["finalized"] = {"visits": V, "margin": args.margin,
                              "deepPlies": args.deep_plies}
    p["meta"]["koSuspect"] = _any_ko(p)
    return p, ""


def _solution_block(kg, stones, base_moves, mover, other, board, target,
                    solset, color, args, region, size, rules, komi, log):
    """Build a solution block with a DEEP main line for each confirmed move.
    The base is a static position, so we analyse `mover`-to-move by inserting
    a single pass for `other` (never two passes)."""
    prefix = list(base_moves)
    lines = []
    for d in solset:
        sc, seq = follow_pv(kg, stones, prefix, mover, d["move"], target,
                            color,
                            size=size, rules=rules, komi=komi,
                            visits=args.visits, region=region,
                            max_plies=args.deep_plies)
        lines.append({"first": d["move"], "seq": seq,
                      "groupScore": round(sc, 3) if sc is not None
                      else d["score"]})
        if log and sc is not None:
            log(f"    deep {mover} {d['move']}: {len(seq)} plies -> {sc:+.2f}")
    return {"toMove": mover, "moves": [d["move"] for d in solset],
            "lines": lines}


def _cap(lines, max_lines):
    """Shortlist for the sidebar: the most instructive (closest-call) lines
    first.  The full set stays in weakSolution for click-through."""
    ts = sorted(lines, key=lambda l: abs(l["groupScore"]))
    return [{"seq": l["seq"], "groupScore": l["groupScore"],
             "result": l["result"]} for l in ts[:max_lines]]


def _any_ko(p):
    for blk in ("killing", "living"):
        for l in p.get(blk, {}).get("lines", []):
            pass
    ws = p.get("weakSolution", {})
    for key in ws:
        for t in ws[key]:
            if t.get("ko"):
                return True
    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--katago", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--dir", nargs="+", default=["accepted"],
                    help="source director(ies) to finalize, e.g. "
                         "'--dir accepted candidates'. Each gets its own "
                         "manifest: accepted -> manifest.json, candidates "
                         "-> manifest-candidates.json")
    ap.add_argument("--visits", type=int, default=5000,
                    help="visits for every re-verification / readout query")
    ap.add_argument("--policy-top", type=int, default=5,
                    help="also try the engine's N highest-visit moves at "
                         "every node, however far from the group they are. "
                         "Distance alone prunes real solutions (distant "
                         "placements, ladder breakers); 0 = local moves only")
    ap.add_argument("--margin", type=int, default=2,
                    help="region margin for enumerated first moves")
    ap.add_argument("--deep-plies", type=int, default=24,
                    help="max plies to extend each main solution line")
    ap.add_argument("--weak-depth", type=int, default=3,
                    help="how many moves deep the challenger may keep "
                         "trying: 1 = a single try + refutation, 3 = try, "
                         "refutation, try again, refutation, try again... "
                         "until convinced")
    ap.add_argument("--weak-branch", type=int, default=3,
                    help="at each level, how many of the most tempting "
                         "failures are continued deeper (bounds the tree)")
    ap.add_argument("--weak-visits", type=int, default=1500,
                    help="visits for the weak-solution dialogue; lower than "
                         "--visits because it is only refuting bad moves "
                         "(0 = use --visits)")
    ap.add_argument("--max-lines", type=int, default=60,
                    help="cap on stored weak-solution explanation lines")
    ap.add_argument("--thresh", type=float, default=0.90,
                    help="|ownership| for a move to count as killing/living")
    ap.add_argument("--settled-check", type=float, default=0.75,
                    help="|ownership| every challenger reply must maintain "
                         "for a settled (dead/alive) claim to stand")
    ap.add_argument("--force", action="store_true",
                    help="re-finalize problems that are already listed in "
                         "their folder's manifest (default: skip them)")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue past a file that errors instead of "
                         "aborting the whole run")
    ap.add_argument("--analysis-threads", type=int, default=8,
                    help="KataGo numAnalysisThreads override: how many of "
                         "the batched queries the engine searches at once. "
                         "Higher keeps the NN batch fuller (a single query "
                         "rarely fills nnMaxBatchSize). Try 4-16; also raise "
                         "nnMaxBatchSize in analysis.cfg to match.")
    ap.add_argument("--verbose", action="store_true",
                    help="print every enumerated move and readout (very "
                         "chatty); default is one line per problem")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing but the final summary")
    args = ap.parse_args()

    # `vlog` = fine-grained per-move detail (verbose only)
    # `info` = one line per problem (default on, silenced by --quiet)
    vlog = ((lambda *a: print(*a, file=sys.stderr)) if args.verbose
            else (lambda *a: None))
    info = ((lambda *a: None) if args.quiet
            else (lambda *a: print(*a, file=sys.stderr)))
    sources = []
    for d in args.dir:
        pth = Path(d) if Path(d).is_absolute() else (ROOT / d)
        fs = sorted(pth.glob("*.json"))
        if not fs:
            print(f"skipping {pth}/ (no *.json)", file=sys.stderr)
            continue
        sources.append((pth, fs))
    if not sources:
        print("nothing to do", file=sys.stderr)
        return
    total = sum(len(fs) for _, fs in sources)
    print(f"finalizing {total} problem(s) from "
          f"{', '.join(str(s) for s, _ in sources)} at {args.visits} "
          f"visits...", file=sys.stderr)
    kg = KataGo(args.katago, args.config, args.model,
                extra_args=["-override-config",
                            f"numAnalysisThreads={args.analysis_threads}"])
    wait_ready(kg)
    kept = deleted = skipped = 0
    written = {}
    for src, files in sources:
        mpath = manifest_for(src.name)
        manifest = load_manifest(mpath)
        skipped_before = skipped
        info(f"--- {src.name}/ -> {mpath.name} ({len(files)} file(s))")
        for f in files:
            # already finalized (and still published)?  Skip it: a problem
            # finalized in candidates/ keeps its entry when it is accepted,
            # so re-running here must not redo the expensive work.
            if (f.name in manifest and (PUB / f.name).exists()
                    and not args.force):
                vlog(f"[{f.name}] already finalized — skipping")
                skipped += 1
                continue
            status = json.loads(f.read_text()).get("status", "?")
            try:
                out, why = finalize(kg, f, args, vlog)
            except Exception as e:                   # noqa: BLE001
                if args.keep_going:
                    info(f"[{f.name}] {status}: ERROR ({e}) — left unchanged")
                    kept += 1
                    continue
                kg.close()
                raise
            if out is None:
                f.unlink()
                unpublish(f.name)
                if f.name in manifest:
                    manifest.remove(f.name)
                save_manifest(manifest, mpath)
                deleted += 1
                info(f"[{f.name}] {status}: DELETED — {why}")
            else:
                f.write_text(json.dumps(out, indent=1))
                publish(out, f.name)
                manifest.append(f.name)
                # written after EVERY problem: a crash mid-batch still
                # leaves a manifest matching what is actually published
                save_manifest(manifest, mpath)
                kb = (PUB / f.name).stat().st_size / 1024
                kept += 1
                nlines = sum(len(v) for v in out.get("weakSolution", {}).values())
                info(f"[{f.name}] {status}: ok — {nlines} lines, "
                     f"{kb:.0f} KB")
        if skipped_here := skipped - skipped_before:
            info(f"    ({skipped_here} already finalized, skipped)")
        written[mpath] = save_manifest(manifest, mpath)
    kg.close()

    for mpath, n in written.items():
        print(f"manifest: {n} problem(s) listed in {mpath}", file=sys.stderr)
    print(f"done: {kept} finalized, {deleted} deleted, "
          f"{skipped} already done", file=sys.stderr)


if __name__ == "__main__":
    main()
