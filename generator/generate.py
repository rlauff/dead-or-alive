#!/usr/bin/env python3
"""Generate life-and-death problems from KataGo self-play.

Usage (see README.md for the full setup):

    python3 generator/generate.py \
        --katago ~/katago/katago \
        --model  ~/katago/kata1-b18c384nbt-latest.bin.gz \
        --config ~/katago/analysis.cfg \
        --games 20 --out candidates

Candidates are written as JSON files into --out; run review_server.py in a
second terminal and review them in the browser as they appear.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

from board import Board, gtp_to_xy, opposite
from detect import (DeathWatcher, validate_event, build_problem_set,
                    pass_probe, validate_probe)

ROOT = Path(__file__).resolve().parent.parent   # project root
SEEN_BASES: set[str] = set()   # base-position hashes emitted this run
from katago_client import KataGo, wait_ready


def pick_move(resp, move_number: int, temperature_moves: int,
              contested_bias: bool = True) -> str | None:
    """Choose the next self-play move.  Early on, sample among near-best
    moves (opening diversity); later, play the top move.  Low visit counts
    already provide the 'mistakes' that create life-and-death swings.
    With contested_bias, sampling prefers moves in areas whose ownership
    is still undecided — where the fights are."""
    infos = resp.get("moveInfos", [])
    if not infos:
        return None
    infos = sorted(infos, key=lambda mi: -mi.get("visits", 0))
    best_wr = infos[0].get("winrate", 0.5)
    if move_number < temperature_moves:
        pool = [mi for mi in infos
                if mi.get("visits", 0) >= 2 and abs(best_wr - mi.get("winrate", 0)) < 0.04]
        if not pool:
            pool = infos[:1]
        own = resp.get("ownershipBlack")
        size = int(math.isqrt(len(own))) if own else 0

        def w(mi):
            base = math.sqrt(mi.get("visits", 1))
            if contested_bias and own and mi["move"].lower() != "pass":
                xy = gtp_to_xy(mi["move"], size)
                if xy:
                    # 1.0 at fully contested points, 0.3 in settled areas
                    base *= 0.3 + 0.7 * (1.0 - abs(own[xy[1] * size + xy[0]]))
            return base
        return random.choices(pool, weights=[w(mi) for mi in pool], k=1)[0]["move"]
    return infos[0]["move"]


def pick_invasion(resp, to_move: str, size: int) -> str | None:
    """Pick a plausible invasion point deep in the OPPONENT's sphere: an
    empty point whose ownership is clearly against the mover, off the
    first line, weighted by the raw policy so the invasion is one a
    player might actually try (3-3 under a star point, a shoulder-in,
    an attachment...).  The opponent will wall it in — and the invader
    must live by making eyes.  Returns None if nothing qualifies."""
    own = resp.get("ownershipBlack")
    pol = resp.get("policy")
    if not own:
        return None
    sign = 1.0 if to_move == "B" else -1.0
    cands = []
    for y in range(1, size - 1):
        for x in range(1, size - 1):
            v = own[y * size + x] * sign
            if v <= -0.55:                     # deep in enemy influence
                p = pol[y * size + x] if pol else 1e-4
                if p is not None and p > 1e-5:
                    cands.append((x, y, max(p, 1e-5)))
    if len(cands) < 4:                          # opponent sphere too thin
        return None
    xs = random.choices(cands, weights=[c[2] for c in cands], k=1)[0]
    from board import xy_to_gtp
    return xy_to_gtp(xs[0], xs[1], size)



def _reject_key(why: str) -> str:
    """Bucket a rejection reason for the per-game summary."""
    w = why.lower()
    for key, hit in (("screen", "screen"), ("open", "open"),
                     ("eyespace", "eyespace"), ("libs", "liberties"),
                     ("locality", "local"), ("quality", "quality"),
                     ("connect", "connect"), ("cut", "cut"),
                     ("many-sols", "too many"), ("region", "region"),
                     ("dup", "duplicate"), ("ko", "ko"),
                     ("group", "group")):
        if hit in w:
            return key
    return "other"


def note_reject(st, why, game_idx, vlog):
    st["rejects"][_reject_key(why)] = st["rejects"].get(_reject_key(why), 0) + 1
    vlog(f"[game {game_idx}]   rejected: {why}")


def emit(kg, hinge, prefix, game_idx, args, out_dir, st, vlog) -> int:
    """Build and write the problem set for a validated hinge.  Returns how
    many files were written.  Shared by the flip watcher and the pass
    probe."""
    log = vlog if args.verbose else None
    probs, why = build_problem_set(
        kg, hinge, size=args.size, rules=args.rules, komi=args.komi,
        visits=args.enum_visits or args.analysis_visits,
        prefix=prefix, cfg=args, log=log)
    if why:
        note_reject(st, why, game_idx, vlog)
        return 0
    base_hash = probs[0]["id"].rsplit("_", 1)[-1]
    if base_hash in SEEN_BASES:
        note_reject(st, "duplicate base position", game_idx, vlog)
        return 0
    SEEN_BASES.add(base_hash)
    n = 0
    for p in probs:
        path = out_dir / f"{p['id']}.json"
        path.write_text(json.dumps(p, indent=1))
        n += 1
        # problems written are ALWAYS reported, verbose or not
        print(f"[game {game_idx}] wrote {path.name} "
              f"({p['status']}, quality {p['meta']['quality']}"
              f"{', pass-alive' if p['meta'].get('bensonAlive') else ''}"
              f"{', KO' if p['meta'].get('koSuspect') else ''})",
              file=sys.stderr)
    st["hinges"] += 1
    return n


def selfplay_game(kg: KataGo, game_idx: int, args, out_dir: Path) -> int:
    size, rules, komi = args.size, args.rules, args.komi
    board = Board(size)
    hist_boards: list[Board] = [board.copy()]   # hist_boards[t] = pos after t moves
    moves: list[list[str]] = []
    watcher = DeathWatcher(game_idx, size,
                           min_group=args.min_group,
                           dead_thresh=-args.dead_thresh,
                           alive_before=-args.alive_before,
                           lookback=args.lookback)
    passes, emitted = 0, 0
    to_move = "B"
    st = {"flips": 0, "probes": 0, "invasions": 0, "hinges": 0, "rejects": {}}
    vlog = ((lambda *a: print(*a, file=sys.stderr)) if args.verbose
            else (lambda *a: None))

    # asymmetric self-play: one side is deliberately weak (few visits) and
    # gets its groups killed by the strong side.  Same engine, two budgets.
    asym = args.weak_visits is not None
    if asym:
        weak = args.weak_side if args.weak_side != "random" \
            else random.choice(["B", "W"])
        vlog(f"[game {game_idx}] asymmetric: {weak} is weak "
             f"({args.weak_visits}v) vs strong ({args.selfplay_visits}v)")
    else:
        weak = None

    invade_cooldown = 0   # moves remaining before another invasion allowed

    for mv_num in range(1, args.max_moves + 1):
        sp_visits = (args.weak_visits if (asym and to_move == weak)
                     else args.selfplay_visits)
        # Analyze the CURRENT position (after len(moves) moves).  The same
        # response drives both flip detection for this position and the
        # choice of the next move — board and ownership stay aligned.
        resp = kg.query(moves, rules=rules, komi=komi, size=size,
                        max_visits=sp_visits, include_ownership=True,
                        include_policy=True)

        events = watcher.feed(board, resp["ownershipBlack"])
        for ev in events:
            st["flips"] += 1
            vlog(f"[game {game_idx}] position {len(moves)}: {ev.color} group "
                 f"({len(ev.coords_now)} stones) flipped "
                 f"{ev.score_before:+.2f} -> {ev.score_after:+.2f} "
                 f"at move {ev.flip_idx}; validating...")
            hinge, why = validate_event(
                kg, ev, hist_boards, moves, size=size, rules=rules,
                komi=komi, visits=args.analysis_visits,
                thresh=args.settle_thresh,
                min_liberties=args.min_liberties,
                min_eyespace=args.min_eyespace,
                hard_open_eyespace=args.hard_open_eyespace,
                rewind=args.rewind)
            if hinge is None:
                note_reject(st, why, game_idx, vlog)
                continue
            emitted += emit(kg, hinge, f"g{game_idx:03d}m{ev.flip_idx:03d}",
                            game_idx, args, out_dir, st, vlog)

        # ---- pass-probe: groups that are alive as the game stands but die
        # if their owner simply passes (they need another move to live).
        # The flip watcher never sees these, because a defended group's
        # ownership never flips during actual play.  The pass goes only into
        # a THROWAWAY query, never into the real move list, so no pass pair
        # is formed and the engine is never told the game is over.
        if (args.pass_probe and mv_num >= args.probe_after
                and mv_num % args.probe_every == 0 and moves):
            for color, coords, s_b, s_a in pass_probe(
                    kg, moves, board, resp["ownershipBlack"], size=size,
                    rules=rules, komi=komi, probe_visits=args.probe_visits,
                    min_group=args.min_group, claimed=watcher.claimed):
                st["probes"] += 1
                vlog(f"[game {game_idx}] pass-probe @ move {len(moves)}: "
                     f"{color} group ({len(coords)} stones) "
                     f"{s_b:+.2f} -> {s_a:+.2f} on pass; validating...")
                hinge, why = validate_probe(
                    kg, coords, color, hist_boards, moves, size=size,
                    rules=rules, komi=komi, visits=args.analysis_visits,
                    thresh=args.settle_thresh,
                    min_liberties=args.min_liberties,
                    min_eyespace=args.min_eyespace,
                    hard_open_eyespace=args.hard_open_eyespace,
                    game_idx=game_idx)
                if hinge is None:
                    note_reject(st, why, game_idx, vlog)
                    continue
                watcher.claimed.append(coords)
                emitted += emit(kg, hinge, f"g{game_idx:03d}p{len(moves):03d}",
                                game_idx, args, out_dir, st, vlog)

        # now pick and play the next move.  With some probability, force a
        # plausible INVASION deep into the opponent's sphere instead: the
        # opponent walls it in and the invader has to live by making eyes
        # — which is exactly the kind of position tsumego come from.  A
        # cooldown ensures only ONE invasion is in flight at a time, so the
        # invaded group can actually settle into a readable shape before the
        # next invasion is attempted.  In asymmetric mode only the WEAK side
        # is allowed to invade (its group is the one that will die).
        move = None
        if invade_cooldown > 0:
            invade_cooldown -= 1
        may_invade = (args.invade_prob > 0 and invade_cooldown == 0
                      and args.invade_after <= mv_num <= args.invade_until
                      and (not asym or to_move == weak))
        if may_invade and random.random() < args.invade_prob:
            move = pick_invasion(resp, to_move, size)
            if move is not None and board.get(*gtp_to_xy(move, size)) == Board.EMPTY:
                st["invasions"] += 1
                vlog(f"[game {game_idx}] move {mv_num}: {to_move} invades "
                     f"at {move}")
                invade_cooldown = args.invade_cooldown
            else:
                move = None
        if move is None:
            move = pick_move(resp, mv_num, args.temperature_moves,
                             contested_bias=not args.no_contested_bias)
        if move is None:
            break
        moves.append([to_move, move])
        if move.lower() == "pass":
            passes += 1
            if passes >= 2:
                break
        else:
            passes = 0
            board.play(to_move, move)
        hist_boards.append(board.copy())
        to_move = opposite(to_move)
    return emitted, st


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--katago", required=True, help="path to katago binary")
    ap.add_argument("--model", required=True, help="path to .bin.gz network")
    ap.add_argument("--config", required=True, help="analysis .cfg file")
    ap.add_argument("--out", default=None,
                    help="candidate output dir (default: <project>/candidates)")
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--size", type=int, default=19)
    ap.add_argument("--preset", choices=["auto", "9x9", "13x13", "19x19"],
                    default="auto",
                    help="board-appropriate defaults for komi, max-moves, "
                         "locality, invasion timing and min-group. 'auto' "
                         "(default) derives the preset from --size. Any knob "
                         "you pass explicitly still overrides the preset.")
    ap.add_argument("--rules", default="chinese")
    ap.add_argument("--komi", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    # self-play knobs
    ap.add_argument("--selfplay-visits", type=int, default=48,
                    help="visits per self-play move (low = fast + blunders). "
                         "In symmetric mode both sides use this; in "
                         "asymmetric mode (--weak-visits) this is the STRONG "
                         "side's budget")
    ap.add_argument("--weak-visits", type=int, default=None,
                    help="enable ASYMMETRIC self-play: the disadvantaged "
                         "side searches only this many visits (e.g. 20) "
                         "while the other uses --selfplay-visits (e.g. 250). "
                         "The strong side systematically kills the weak "
                         "side's groups, yielding many clean dead-group "
                         "problems. Off by default (symmetric)")
    ap.add_argument("--weak-side", choices=["B", "W", "random"],
                    default="random",
                    help="which colour is the WEAK (low-visit) side in "
                         "asymmetric mode; 'random' picks per game")
    ap.add_argument("--max-moves", type=int, default=None,
                    help="hard cap on self-play moves per game "
                         "(preset-derived if unset)")
    ap.add_argument("--temperature-moves", type=int, default=30,
                    help="sample among near-best moves for this many moves")
    # detection knobs
    ap.add_argument("--min-group", type=int, default=None,
                    help="minimum stones for a group to count "
                         "(preset-derived if unset)")
    ap.add_argument("--dead-thresh", type=float, default=0.95,
                    help="|ownership| for 'settled dead' during self-play")
    ap.add_argument("--alive-before", type=float, default=0.5,
                    help="group must have been above -X recently")
    ap.add_argument("--lookback", type=int, default=10)
    # validation knobs
    ap.add_argument("--analysis-visits", type=int, default=600,
                    help="visits for the deep validation queries")
    ap.add_argument("--settle-thresh", type=float, default=0.90,
                    help="ownership needed to call a candidate move "
                         "killing/living")
    ap.add_argument("--settled-check", type=float, default=0.70,
                    help="dead/alive variants are only emitted if EVERY "
                         "local challenge keeps the group beyond this "
                         "|ownership| in the settled direction")
    # quality / enumeration knobs (the whole enumeration is built into
    # generation: every empty point of the region is deep-checked)
    ap.add_argument("--enum-visits", type=int, default=None,
                    help="visits per enumeration query "
                         "(default: --analysis-visits)")
    ap.add_argument("--policy-top", type=int, default=5,
                    help="also try the engine's N highest-visit moves when "
                         "looking for killing/living moves, however far from "
                         "the group (a vital point is not always nearby); "
                         "0 = local moves only")
    ap.add_argument("--margin", type=int, default=2,
                    help="region margin around the group; all quality "
                         "checks and tried moves live inside this bbox")
    ap.add_argument("--max-solutions", type=int, default=3,
                    help="reject hinges with more killing (or living) "
                         "moves than this — many answers = mushy problem")
    ap.add_argument("--max-region-area", type=int, default=56,
                    help="SOFT: regions larger than this lose quality "
                         "points (roomy problems still pass, just scored "
                         "lower); the actual reject is --hard-max-region-area")
    ap.add_argument("--hard-max-region-area", type=int, default=140,
                    help="HARD reject: region bbox larger than this "
                         "(enumeration cost — one deep query per empty "
                         "point in the region)")
    ap.add_argument("--max-group", type=int, default=16,
                    help="SOFT: groups larger than this lose quality points; "
                         "the actual reject is --hard-max-group")
    ap.add_argument("--hard-max-group", type=int, default=40,
                    help="HARD reject: groups with more stones than this")
    ap.add_argument("--locality", type=float, default=None,
                    help="max mean |ownership| swing OUTSIDE the region "
                         "between the kill and live branches; larger = the "
                         "fight entangles the whole board -> rejected. "
                         "Preset-derived if unset (small boards need a "
                         "looser value since everything is 'near' the fight)")
    ap.add_argument("--min-quality", type=int, default=40,
                    help="reject hinges scoring below this (0-100)")
    ap.add_argument("--max-lines", type=int, default=24,
                    help="cap on stored explanation lines per problem")
    ap.add_argument("--allow-ko", action="store_true",
                    help="keep problems whose solutions or refutations run "
                         "through a ko (flagged koSuspect) instead of "
                         "rejecting them")
    ap.add_argument("--allow-connect", action="store_true",
                    help="keep hinges whose solution is a connect-out or a "
                         "cut (rejected by default as trivial)")
    # shape gates: what separates a tsumego from an incident
    ap.add_argument("--min-liberties", type=int, default=3,
                    help="reject groups with fewer liberties at the base "
                         "(near-captures are not problems)")
    ap.add_argument("--min-eyespace", type=int, default=3,
                    help="minimum enclosed eyespace; 1-2 points means a "
                         "trivial capture or single connect/cut")
    ap.add_argument("--rewind", type=int, default=4,
                    help="how many positions to walk back from the observed "
                         "death looking for the still-savable hinge. The "
                         "killing move usually lands a ply or two before "
                         "ownership flips, so the exact flip-1 position is "
                         "often already lost; a larger rewind finds more "
                         "hinges (at more screening queries)")
    ap.add_argument("--hard-open-eyespace", type=int, default=30,
                    help="HARD reject: a group whose reachable empty space "
                         "exceeds this is OPEN (can run) — a middlegame "
                         "fight, not L&D. Set high; roomy enclosed shapes "
                         "(rectangular six, comb, big invasions) are fine "
                         "and only cost quality points, not a rejection")
    # self-play steering: manufacture eye-making fights
    ap.add_argument("--invade-prob", type=float, default=0.05,
                    help="per-move probability of forcing a plausible "
                         "invasion deep into the opponent's sphere (subject "
                         "to the cooldown below); the resulting walled-in "
                         "group must live by making eyes (0 to disable). "
                         "Keep this low — a few invasions per game is plenty")
    ap.add_argument("--invade-cooldown", type=int, default=25,
                    help="minimum moves between forced invasions, so at most "
                         "one invaded group is in flight at a time and it "
                         "can settle into a readable shape")
    ap.add_argument("--invade-after", type=int, default=None,
                    help="no forced invasions before this move "
                         "(preset-derived if unset)")
    ap.add_argument("--invade-until", type=int, default=None,
                    help="no forced invasions after this move "
                         "(preset-derived if unset)")
    ap.add_argument("--pass-probe", action="store_true",
                    help="each probed move, force the side-to-move to pass "
                         "in a THROWAWAY query and check whether any of "
                         "their large groups dies; if so, the current "
                         "position is a hinge (the group needs a move to "
                         "live). Catches problems the flip watcher never "
                         "sees. One extra query per probed move.")
    ap.add_argument("--probe-every", type=int, default=4,
                    help="run the pass-probe every Nth move")
    ap.add_argument("--probe-after", type=int, default=20,
                    help="no pass-probing before this move")
    ap.add_argument("--probe-visits", type=int, default=None,
                    help="visits for the throwaway pass query "
                         "(default: --selfplay-visits)")
    ap.add_argument("--no-contested-bias", action="store_true",
                    help="disable the sampling bias toward moves in "
                         "contested (undecided-ownership) areas")
    ap.add_argument("--verbose", action="store_true",
                    help="log every enumerated try")
    args = ap.parse_args()

    # ---- resolve board-size presets --------------------------------------
    # Each preset supplies defaults for the size-sensitive knobs; anything
    # the user passed explicitly (i.e. is not None) wins.
    PRESETS = {
        # komi, max_moves, min_group, locality, invade_after, invade_until
        "9x9":   dict(komi=7.0,  max_moves=90,  min_group=3,
                      locality=0.45, invade_after=6,  invade_until=55),
        "13x13": dict(komi=7.5,  max_moves=170, min_group=4,
                      locality=0.30, invade_after=10, invade_until=110),
        "19x19": dict(komi=7.5,  max_moves=320, min_group=5,
                      locality=0.15, invade_after=14, invade_until=200),
    }
    preset_name = args.preset
    if preset_name == "auto":
        preset_name = ("9x9" if args.size <= 9 else
                       "13x13" if args.size <= 13 else "19x19")
    preset = PRESETS[preset_name]
    for k, v in preset.items():
        if getattr(args, k) is None:
            setattr(args, k, v)
    print(f"[preset] {preset_name} (size {args.size}): komi {args.komi}, "
          f"max-moves {args.max_moves}, min-group {args.min_group}, "
          f"locality {args.locality}, invade {args.invade_after}-"
          f"{args.invade_until}", file=sys.stderr)

    if args.probe_visits is None:
        args.probe_visits = args.selfplay_visits
    if args.seed is not None:
        random.seed(args.seed)
    out_dir = Path(args.out) if args.out else ROOT / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[paths] candidates -> {out_dir.resolve()}", file=sys.stderr)
    print(f"[paths] review with: python3 {ROOT / 'review_server.py'} "
          f"(accepted problems land in {ROOT / 'accepted'})", file=sys.stderr)

    kg = KataGo(args.katago, args.config, args.model, log_stderr=False)
    try:
        print("[init] waiting for KataGo (first query compiles/tunes the GPU "
              "backend; can take a while on first run)...", file=sys.stderr)
        wait_ready(kg)
        kg.calibrate()
        total = 0
        for g in range(1, args.games + 1):
            t0 = time.time()
            n, st = selfplay_game(kg, g, args, out_dir)
            total += n
            rej = ", ".join(f"{k} {v}" for k, v in
                            sorted(st["rejects"].items(), key=lambda kv: -kv[1]))
            bits = [f"{st['flips']} flips"]
            if args.pass_probe:
                bits.append(f"{st['probes']} probes")
            if st["invasions"]:
                bits.append(f"{st['invasions']} inv")
            bits.append(f"{st['hinges']} hinges")
            print(f"[game {g}] {time.time() - t0:.0f}s · {' · '.join(bits)} "
                  f"· {n} problems (total {total})"
                  + (f" · rejected: {rej}" if rej else ""), file=sys.stderr)
        print(f"[done] {total} candidate problems in {out_dir}/", file=sys.stderr)
    finally:
        kg.close()


if __name__ == "__main__":
    main()
