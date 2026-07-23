"""Turn ownership flips observed during self-play into reviewed problems.

Pipeline per detected event:

  1. During self-play we watch the ownership map after every move.  A group
     whose mean ownership (from its owner's perspective) was >= -alive_before
     recently but is <= -dead_thresh now has just "died" according to the net.
  2. We rewind to the position BEFORE the move that flipped it (the "base
     position") and re-analyze deeply, twice:
       - attacker to move  -> which first moves kill the group?
       - defender to move (attacker passes if needed) -> which first moves live?
  3. If both a killing and a living move exist, the base position is a
     genuine life-and-death hinge.  We emit up to three problems:
       - "undecided": the base position itself
       - "dead":      base + best killing move played (only if no defender
                      reply revives the group in a deep re-check)
       - "alive":     base + best living move played (only if no attacker
                      reply kills the group in a deep re-check)
     each with precomputed main lines for exploration.

Indexing convention: "position t" is the position after t moves; the move
that creates position t is move #t (1-based over the game record).
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field

from board import Board, gtp_to_xy, xy_to_gtp, opposite


# ----------------------------------------------------------------- ownership
def group_mean_own(coords, own_black, size, color) -> float:
    """Mean ownership over `coords` from the perspective of `color`.
    +1 = the group's owner holds these points, -1 = the opponent does."""
    sign = 1.0 if color == "B" else -1.0
    vals = [sign * own_black[y * size + x] for (x, y) in coords]
    return sum(vals) / len(vals) if vals else 0.0

def bbox(coords, size, margin=2):
    xs = [x for x, _ in coords]
    ys = [y for _, y in coords]
    return {
        "x0": max(0, min(xs) - margin),
        "y0": max(0, min(ys) - margin),
        "x1": min(size - 1, max(xs) + margin),
        "y1": min(size - 1, max(ys) + margin),
    }


def local_empty_points(board: Board, target, size: int, margin: int = 2):
    """GTP coordinates of all empty points in the bbox around `target` —
    the plausible first moves of a local life-and-death attempt."""
    r = bbox(target, size, margin)
    out = []
    for y in range(r["y0"], r["y1"] + 1):
        for x in range(r["x0"], r["x1"] + 1):
            if board.get(x, y) == Board.EMPTY:
                out.append(xy_to_gtp(x, y, size))
    return out


def line_has_ko(start_board: Board, seq) -> bool:
    """Replay `seq` on a copy of `start_board` and detect a ko recapture:
    a single-stone capture immediately followed by the opponent capturing a
    single stone back on the just-vacated point.  Lines containing this are
    ko fights — a status resting on them is NOT unconditional."""
    b = start_board.copy()
    last_single: tuple[int, int] | None = None   # point vacated by a 1-stone capture
    for color, mv in seq:
        xy = gtp_to_xy(mv, b.size)
        if xy is None:
            last_single = None
            continue
        x, y = xy
        if b.get(x, y) != Board.EMPTY:
            return False        # replay diverged; don't guess
        opp = opposite(color)
        b.grid[y][x] = color
        captured: list[tuple[int, int]] = []
        for nx, ny in b.neighbors(x, y):
            if b.get(nx, ny) == opp:
                st, li = b.group_at(nx, ny)
                if not li:
                    for s in st:
                        b.grid[s[1]][s[0]] = Board.EMPTY
                    captured.extend(st)
        st, li = b.group_at(x, y)
        if not li:              # suicide safety net
            for s in st:
                b.grid[s[1]][s[0]] = Board.EMPTY
            continue
        if (last_single is not None and (x, y) == last_single
                and len(captured) == 1):
            return True         # recaptured the ko
        last_single = captured[0] if (len(captured) == 1
                                      and len(st) == 1) else None
    return False


def trim_line_to_region(seq, region: dict, size: int, slack: int = 1):
    """Cut a line at the first move that leaves the (slightly enlarged)
    problem region — beyond that it's whole-board endgame, not the fight."""
    x0 = max(0, region["x0"] - slack); y0 = max(0, region["y0"] - slack)
    x1 = min(size - 1, region["x1"] + slack); y1 = min(size - 1, region["y1"] + slack)
    out = []
    for color, mv in seq:
        xy = gtp_to_xy(mv, size)
        if xy is None:
            break
        if not (x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1):
            break
        out.append([color, mv])
    return out


# ------------------------------------------------------------- shape checks
def eyespace(board: Board, target, cap: int):
    """The target group's potential eyespace: all empty points reachable
    from its liberties through empty points.  The flood fill stops once it
    exceeds `cap` points — a fill that large means the group is OPEN (it
    can run or has an escape gap), which is a running fight, not a
    tsumego.  Returns (points:set, open:bool)."""
    color = board.get(*target[0])
    seeds = set()
    for (x, y) in target:
        for nx, ny in board.neighbors(x, y):
            if board.get(nx, ny) == Board.EMPTY:
                seeds.add((nx, ny))
    seen, stack = set(seeds), list(seeds)
    while stack:
        x, y = stack.pop()
        if len(seen) > cap:
            return seen, True
        for nx, ny in board.neighbors(x, y):
            if board.get(nx, ny) == Board.EMPTY and (nx, ny) not in seen:
                seen.add((nx, ny))
                stack.append((nx, ny))
    return seen, False


def eyespace_purity(board: Board, space, target, color) -> tuple[float, int]:
    """How cleanly the eyespace is enclosed: fraction of its non-empty
    border that is attacker stones or the board edge, and the number of
    distinct FRIENDLY non-target groups touching it (connection outlets)."""
    atk = opposite(color)
    tset = set(target)
    wall = friendly = 0
    outlets: set[tuple[int, int]] = set()
    edge = 0
    for (x, y) in space:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not board.in_bounds(nx, ny):
                edge += 1
                continue
            c = board.get(nx, ny)
            if c == atk:
                wall += 1
            elif c == color and (nx, ny) not in tset:
                friendly += 1
                st, _ = board.group_at(nx, ny)
                outlets.add(min(st))
    denom = wall + friendly + edge
    return ((wall + edge) / denom if denom else 1.0), len(outlets)


def move_is_cut(board: Board, mv: str, target, color, region) -> bool:
    """True if the killing move mainly works as a cut: it is adjacent to a
    friendly (defender-colored) non-target group that reaches outside the
    problem region — i.e. the 'problem' is separating the group from
    support, not killing its eyespace."""
    xy = gtp_to_xy(mv, board.size)
    if xy is None:
        return False
    tset = set(target)
    for nx, ny in board.neighbors(*xy):
        if board.get(nx, ny) == color and (nx, ny) not in tset:
            st, _ = board.group_at(nx, ny)
            if any(not (region["x0"] <= sx <= region["x1"]
                        and region["y0"] <= sy <= region["y1"])
                   for (sx, sy) in st):
                return True
    return False


def move_is_connect_out(board: Board, mv: str, target, color, region) -> bool:
    """True if the living move works by connecting the group to an outside
    friendly group: after playing it the merged group contains stones
    beyond the problem region.  Living by linking out is not eye-making."""
    xy = gtp_to_xy(mv, board.size)
    if xy is None:
        return False
    b = board.copy()
    b.play(color, mv)
    if b.get(*xy) != color:
        return False
    st, _ = b.group_at(*xy)
    tset = set(target) | {xy}
    for (sx, sy) in st:
        if (sx, sy) in tset:
            continue
        if not (region["x0"] <= sx <= region["x1"]
                and region["y0"] <= sy <= region["y1"]):
            return True
    return False


# ------------------------------------------------------------------- events
@dataclass
class DeathEvent:
    game_idx: int
    flip_idx: int              # position index t whose creating move flipped it
    color: str                 # color of the dying group
    coords_now: frozenset      # stones of the group when detected
    score_before: float
    score_after: float


class DeathWatcher:
    """Feed it (board, ownershipBlack) for every position of the game, in
    order, STARTING WITH THE EMPTY BOARD (position 0).  Yields DeathEvents
    for groups that recently flipped to dead."""

    def __init__(self, game_idx: int, size: int, *, min_group: int = 4,
                 dead_thresh: float = -0.95, alive_before: float = -0.5,
                 lookback: int = 10):
        self.game_idx = game_idx
        self.size = size
        self.min_group = min_group
        self.dead_thresh = dead_thresh
        self.alive_before = alive_before
        self.lookback = lookback
        self.hist: list[tuple[Board, list[float]]] = []   # hist[t] = pos t
        self.claimed: list[frozenset] = []                # regions reported

    def _overlaps_claimed(self, coords: frozenset) -> bool:
        for c in self.claimed:
            inter = len(coords & c)
            if inter and inter >= 0.3 * min(len(coords), len(c)):
                return True
        return False

    def feed(self, board: Board, own_black: list[float]):
        """Record position t = len(hist) and return any new DeathEvents."""
        t = len(self.hist)
        self.hist.append((board.copy(), own_black))
        events = []
        for color, stones in board.all_groups():
            if len(stones) < self.min_group:
                continue
            fs = frozenset(stones)
            if self._overlaps_claimed(fs):
                continue
            s_now = group_mean_own(stones, own_black, self.size, color)
            if s_now > self.dead_thresh:
                continue
            # trace the same points back through recent positions (counting
            # only points that actually held this color back then)
            hist_scores: list[tuple[int, float | None]] = []
            for j in range(max(0, t - self.lookback), t):
                b_j, own_j = self.hist[j]
                coords_j = [(x, y) for (x, y) in stones if b_j.get(x, y) == color]
                if len(coords_j) < self.min_group:
                    hist_scores.append((j, None))
                    continue
                hist_scores.append(
                    (j, group_mean_own(coords_j, own_j, self.size, color)))
            healthy = [j for j, s in hist_scores
                       if s is not None and s >= self.alive_before]
            if not healthy:
                continue
            j0 = max(healthy)
            # first position after j0 that is (near) dead
            flip = t
            for j, s in hist_scores:
                if j > j0 and s is not None and s <= self.dead_thresh + 0.05:
                    flip = j
                    break
            self.claimed.append(fs)
            events.append(DeathEvent(self.game_idx, flip, color, fs,
                                     score_before=dict(hist_scores)[j0],
                                     score_after=s_now))
        return events


# --------------------------------------------------------------- validation
@dataclass
class Hinge:
    """A validated life-and-death hinge position."""
    base_moves: list[list[str]]      # moves from the empty board to the base
    base_board: Board
    color: str                       # color of the target group
    target: list[tuple[int, int]]    # its stones at the base position
    killing: list[dict] = field(default_factory=list)  # enumeration entries
    living: list[dict] = field(default_factory=list)   # {move, score, seq, ko}
    fails: list[dict] = field(default_factory=list)    # failing local tries
    eyespace: set = field(default_factory=set)         # enclosed empty space
    priors: dict = field(default_factory=dict)         # raw-policy priors
    quality: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


def _pv_to_line(first_player: str, pv: list[str], max_len: int = 12):
    """Label a KataGo pv (which includes the root move itself) with
    alternating colors.  The line is truncated at the first pass: once
    either side passes, the local life-and-death demonstration is over and
    the tail is just whole-board endgame noise."""
    seq, p = [], first_player
    for mv in pv[:max_len]:
        if mv.lower() == "pass":
            break
        seq.append([p, mv])
        p = opposite(p)
    return seq


def _classify_moves(resp, target, size, color, *, want: str, thresh: float,
                    min_visit_frac: float = 0.02, max_keep: int = 4):
    """From a deep analysis with includeMovesOwnership, pick candidate first
    moves that make the target group dead (`want='dead'`) or alive
    (`want='alive'`), each with its principal variation."""
    infos = resp.get("moveInfos", [])
    total = sum(mi.get("visits", 0) for mi in infos) or 1
    out = []
    for mi in infos:
        if "ownershipBlack" not in mi:
            continue
        if mi.get("visits", 0) < max(8, min_visit_frac * total):
            continue
        s = group_mean_own(target, mi["ownershipBlack"], size, color)
        ok = (s <= -thresh) if want == "dead" else (s >= thresh)
        if ok and mi["move"].lower() != "pass":
            out.append({
                "move": mi["move"],
                "groupScore": round(s, 3),
                "visits": mi["visits"],
                "winrate": round(mi.get("winrate", 0.0), 3),
                "pv": mi.get("pv", []),
            })
    out.sort(key=lambda d: -d["visits"])
    return out[:max_keep]


def _try_base(kg, base_idx, hist_boards, moves, color, coords, *, size,
              rules, komi, visits, thresh, min_liberties, min_eyespace,
              hard_open_eyespace):
    """Attempt to treat position `base_idx` as a life-and-death hinge for
    the group `coords` of `color`.  Returns (hinge, "") or (None, reason)."""
    if base_idx < 2:
        return None, "flip too early in the game"
    base_moves = [m[:] for m in moves[:base_idx]]
    base_board = hist_boards[base_idx]
    attacker = opposite(color)
    target = [(x, y) for (x, y) in coords if base_board.get(x, y) == color]
    if len(target) < 3:
        return None, "group not present / < 3 stones at this base"
    _, libs = base_board.group_at(*target[0])
    if len(libs) < min_liberties:
        return None, (f"group has only {len(libs)} liberties — "
                      "an obvious capture, not a problem")
    space, is_open = eyespace(base_board, target, hard_open_eyespace)
    if is_open:
        return None, (f"group is open (reachable empty space > "
                      f"{hard_open_eyespace}) — a middlegame fight")
    if len(space) < min_eyespace:
        return None, (f"eyespace of {len(space)} point(s) — trivial "
                      "capture or single connect/cut")
    to_move = "W" if base_moves[-1][0] == "B" else "B"

    def with_pass(player_needed):
        ms = [m[:] for m in base_moves]
        if to_move != player_needed:
            ms.append([to_move, "pass"])
        return ms

    screen = min(thresh, 0.80)
    att_resp = kg.query(with_pass(attacker), rules=rules, komi=komi,
                        size=size, max_visits=visits,
                        include_moves_ownership=True)
    if not _classify_moves(att_resp, target, size, color,
                           want="dead", thresh=screen):
        return None, "screen: no plausible killing move"
    def_resp = kg.query(with_pass(color), rules=rules, komi=komi, size=size,
                        max_visits=visits, include_moves_ownership=True)
    if not _classify_moves(def_resp, target, size, color,
                           want="alive", thresh=screen):
        return None, "screen: no plausible living move"

    h = Hinge(base_moves=base_moves, base_board=base_board, color=color,
              target=target)
    h.eyespace = space
    h.priors = {
        "att": {mi["move"]: mi.get("prior", 0.0)
                for mi in att_resp.get("moveInfos", [])},
        "def": {mi["move"]: mi.get("prior", 0.0)
                for mi in def_resp.get("moveInfos", [])},
    }
    h.meta = {
        "attackerPassNeeded": to_move != attacker,
        "defenderPassNeeded": to_move != color,
        "eyespaceSize": len(space),
        "liberties": len(libs),
        "baseIndex": base_idx,
    }
    return h, ""


def validate_event(kg, ev: DeathEvent, hist_boards, moves: list[list[str]],
                   *, size: int, rules: str, komi: float, visits: int,
                   thresh: float = 0.90, min_liberties: int = 3,
                   min_eyespace: int = 3, hard_open_eyespace: int = 30,
                   rewind: int = 4) -> tuple["Hinge | None", str]:
    """Cheap screen of a flip event.  The killing move often precedes the
    ownership flip by a ply or two, so the single position `flip-1` may
    already be lost (no living move left).  We therefore walk BACKWARD from
    `flip-1` up to `rewind` positions and return the FIRST that screens as a
    genuine hinge — the last moment the group was still savable.

    Board-shape gates (liberties, enclosure, eyespace size) plus a screen
    for a plausible killing AND living first move.  The exhaustive
    verification, remaining quality gates and problem emission happen in
    build_problem_set().  Returns (hinge, "") or (None, reason)."""
    last_reason = "flip too early in the game"
    top = ev.flip_idx - 1
    for base_idx in range(top, max(1, top - rewind) - 1, -1):
        h, reason = _try_base(
            kg, base_idx, hist_boards, moves, ev.color, ev.coords_now,
            size=size, rules=rules, komi=komi, visits=visits, thresh=thresh,
            min_liberties=min_liberties, min_eyespace=min_eyespace,
            hard_open_eyespace=hard_open_eyespace)
        if h is not None:
            h.meta.update({
                "gameIdx": ev.game_idx,
                "flipMove": ev.flip_idx,
                "scoreBefore": round(ev.score_before, 3),
                "scoreAfter": round(ev.score_after, 3),
                "rewindUsed": top - base_idx,
            })
            return h, ""
        last_reason = reason
    return None, last_reason


def pass_probe(kg, moves, board, own_now, *, size, rules, komi, probe_visits,
               min_group, alive_before=-0.5, dead_after=-0.90, claimed=None):
    """Detect groups that are alive as the game stands but would DIE if
    their owner simply passed — i.e. groups that need another move to live.

    This catches the textbook life-and-death hinge that the flip watcher
    never sees: as long as the owner keeps defending, the group's ownership
    never flips during actual play, so it is invisible to death detection.
    Forcing a *hypothetical* pass reveals it.

    `own_now` is the ownershipBlack of the CURRENT position (owner to move).
    We make the side-to-move pass in a THROWAWAY query (never added to the
    real move list, so no pass pair is ever formed and the engine is not
    told the game is over) and re-read ownership.  Any of that side's groups
    that was healthy (>= alive_before) and is now dead (<= dead_after)
    becomes a candidate; the current position is its hinge base.

    Yields (color, coords:frozenset) candidates; the caller screens each
    with the normal validation path.  `claimed` is an optional list of
    already-emitted regions to skip (dedup)."""
    if not moves:
        return
    to_move = "W" if moves[-1][0] == "B" else "B"
    claimed = claimed or []

    # which of the side-to-move's groups are currently healthy and big?
    live_groups = []
    for color, stones in board.all_groups():
        if color != to_move or len(stones) < min_group:
            continue
        s = group_mean_own(stones, own_now, size, color)
        if s >= alive_before:
            live_groups.append((color, frozenset(stones), s))
    if not live_groups:
        return

    # throwaway pass probe — NOT appended to the real game
    probe = [m[:] for m in moves] + [[to_move, "pass"]]
    resp = kg.query(probe, rules=rules, komi=komi, size=size,
                    max_visits=probe_visits, include_ownership=True)
    own_pass = resp.get("ownershipBlack")
    if not own_pass:
        return

    for color, stones, s_before in live_groups:
        s_after = group_mean_own(list(stones), own_pass, size, color)
        if s_after > dead_after:
            continue                       # survives a pass — not a hinge
        # dedup against already-claimed regions
        skip = False
        for c in claimed:
            inter = len(stones & c)
            if inter and inter >= 0.3 * min(len(stones), len(c)):
                skip = True
                break
        if skip:
            continue
        yield color, stones, round(s_before, 3), round(s_after, 3)


def validate_probe(kg, coords, color, hist_boards, moves, *, size, rules,
                   komi, visits, thresh, min_liberties, min_eyespace,
                   hard_open_eyespace, game_idx):
    """Validate a pass-probe candidate.  The CURRENT position (index
    len(moves)) is the hinge base: the group is alive as it stands and dies
    only if its owner passes, so no rewind is needed.  Reuses the same
    shape gates + kill/live screen as flip-detected events."""
    base_idx = len(moves)
    h, reason = _try_base(
        kg, base_idx, hist_boards, moves, color, coords,
        size=size, rules=rules, komi=komi, visits=visits, thresh=thresh,
        min_liberties=min_liberties, min_eyespace=min_eyespace,
        hard_open_eyespace=hard_open_eyespace)
    if h is not None:
        h.meta.update({"gameIdx": game_idx, "flipMove": base_idx,
                       "source": "pass-probe", "rewindUsed": 0})
    return h, reason


# ----------------------------------------------------------------- problems
def _problem_id(prefix: str, board: Board, kind: str) -> str:
    h = hashlib.sha1("".join("".join(r) for r in board.grid).encode()).hexdigest()[:8]
    return f"{prefix}_{kind}_{h}"


def _base_json(board: Board, size: int, rules: str, komi: float,
               color: str, target, kind: str, to_move: str, prefix: str,
               meta: dict) -> dict:
    tgt = [xy_to_gtp(x, y, size) for (x, y) in target]
    return {
        "id": _problem_id(prefix, board, kind),
        "boardSize": size,
        "rules": rules,
        "komi": komi,
        "initialStones": board.stones_list(),
        "toMove": to_move,
        "status": kind,
        "targetColor": color,
        "targetStones": tgt,
        "region": bbox(target, size),
        "meta": meta,
    }


# ------------------------------------------------------ local enumeration
def enumerate_tries(kg, base_moves, to_move, side, board, target, *,
                    size, rules, komi, visits, margin=2, top_n=5, log=None):
    """Try EVERY empty point in the region around `target` as a first move
    by `side` (a pass is inserted for the other player if needed) and
    deep-evaluate each resulting position.

    Returns a list of dicts, one per legal try:
      {move, score, seq, ko, own}
    where `score` is the target group's mean ownership from its owner's
    perspective under best play after the try, `seq` the labeled line
    ([[side, move]] + engine continuation, trimmed at passes and at the
    first move outside the enlarged region), `ko` whether the line contains
    a ko recapture, and `own` the full ownershipBlack array (used for the
    locality check)."""
    color = board.get(*target[0])
    region = bbox(target, size, margin)
    local = local_empty_points(board, target, size, margin)
    prefix_moves = [m[:] for m in base_moves]
    if to_move != side:
        prefix_moves.append([to_move, "pass"])
    # Also try the moves the engine itself rates highest, however far from
    # the group: a vital point can lie well outside the neighbourhood (a
    # distant eyespace-reducing placement, a ladder breaker), and pruning
    # by distance alone would reject the hinge for want of a killing or
    # living move that does exist.
    if top_n > 0:
        try:
            root = kg.query(prefix_moves, rules=rules, komi=komi, size=size,
                            max_visits=visits, include_ownership=False)
        except RuntimeError:
            root = {}
        seen = set(local)
        for mi in sorted(root.get("moveInfos", []),
                         key=lambda m: -m.get("visits", 0))[:top_n]:
            mv = mi["move"]
            if mv.lower() == "pass" or mv in seen:
                continue
            xy = gtp_to_xy(mv, size)
            if xy and board.get(*xy) == Board.EMPTY:
                seen.add(mv)
                local.append(mv)
                region = {"x0": min(region["x0"], max(0, xy[0] - 1)),
                          "y0": min(region["y0"], max(0, xy[1] - 1)),
                          "x1": max(region["x1"], min(size - 1, xy[0] + 1)),
                          "y1": max(region["y1"], min(size - 1, xy[1] + 1))}
    out = []
    for mv in local:
        try:
            resp = kg.query(prefix_moves + [[side, mv]], rules=rules,
                            komi=komi, size=size, max_visits=visits,
                            include_ownership=True,
                            include_moves_ownership=True)
        except RuntimeError as e:
            if "llegal" in str(e):
                continue                       # e.g. suicide into a real eye
            raise
        own = resp["ownershipBlack"]
        s = group_mean_own(target, own, size, color)
        tail = []
        for mi in sorted(resp.get("moveInfos", []),
                         key=lambda m: -m.get("visits", 0)):
            if mi["move"].lower() == "pass":
                continue
            tail = _pv_to_line(opposite(side), mi.get("pv", []))
            break
        line = trim_line_to_region([[side, mv]] + tail, region, size, slack=1)
        if not line:
            line = [[side, mv]]
        t = {"move": mv, "score": round(s, 3), "seq": line,
             "ko": line_has_ko(board, line), "own": own}
        out.append(t)
        if log:
            log(f"    {side} {mv:>4}: group {s:+.2f}"
                f"{'  [ko]' if t['ko'] else ''}")
    return out


def _lines_of(tries, max_lines=24):
    ts = sorted(tries, key=lambda t: abs(t["score"]))
    return [{"seq": t["seq"], "groupScore": t["score"],
             "result": ("alive" if t["score"] > 0.5 else
                        ("dead" if t["score"] < -0.5 else "unclear"))}
            for t in ts[:max_lines]]


# ------------------------------------------------------------- quality
def quality_score(*, n_kill, n_live, area, max_area, ambiguity,
                  loc_delta, loc_limit, wall_own, group_size,
                  edge_dist, obvious=0.0, purity=1.0, outlets=0,
                  eyespace_size=None, soft_group=16) -> tuple[int, dict]:
    """Heuristic 0-100 quality of a hinge as a tsumego.  Components are
    returned so the review UI can show WHY a problem scored what it did.

    Large groups / regions / eyespaces are NOT disqualifying here — the
    hard rejects live in build_problem_set.  These terms only gently prefer
    compact classic shapes, so a roomy but genuine problem still clears a
    reasonable --min-quality."""
    comp = {}
    q = 100.0
    comp["extraSolutions"] = -10.0 * ((n_kill - 1) + (n_live - 1))
    # gentle, capped region penalty (starts only past `max_area`)
    comp["regionArea"] = -max(0.0, min(15.0, 15.0 * (area - max_area)
                                       / max(1, max_area)))
    comp["ambiguity"] = -30.0 * ambiguity
    comp["locality"] = -min(30.0, 30.0 * loc_delta / max(loc_limit, 1e-6))
    comp["wall"] = -max(0.0, min(20.0, 20.0 * (0.5 - wall_own) / 0.5))
    # gentle group-size penalty, keyed to the soft threshold, capped low
    comp["groupSize"] = -max(0.0, min(8.0, 1.0 * (group_size - soft_group)))
    comp["edge"] = 8.0 if edge_dist == 0 else (0.0 if edge_dist <= 2 else -8.0)
    # a solution the raw policy already names is an easy problem; a
    # deep-search-only vital point is a good one
    comp["obviousness"] = -max(0.0, min(30.0, 45.0 * (obvious - 0.25)))
    # eyespace enclosed by anything other than the attacker wall / edge
    # leaks connections; each friendly outlet is a link-up escape hatch
    comp["enclosure"] = -min(15.0, 40.0 * (1.0 - purity)) - 6.0 * outlets
    if eyespace_size is not None:
        # BONUS for the classic sweet spot (4-8: bent four, rect six, ...);
        # larger enclosed spaces are neutral, not penalized — they are
        # legitimate, just less "textbook"
        comp["eyespace"] = 5.0 if 4 <= eyespace_size <= 8 else 0.0
    for v in comp.values():
        q += v
    return int(max(0, min(100, round(q)))), {k: round(v, 1) for k, v in comp.items()}


def _mean_abs_delta_outside(own_a, own_b, region, size):
    x0, y0, x1, y1 = region["x0"], region["y0"], region["x1"], region["y1"]
    tot, n = 0.0, 0
    for y in range(size):
        for x in range(size):
            if x0 <= x <= x1 and y0 <= y <= y1:
                continue
            tot += abs(own_a[y * size + x] - own_b[y * size + x])
            n += 1
    return tot / n if n else 0.0


# ----------------------------------------------------------- build (v2)
def build_problem_set(kg, h: Hinge, *, size: int, rules: str, komi: float,
                      visits: int, prefix: str, cfg, log=None):
    """Exhaustively enumerate the hinge and emit high-quality problems.

    All three variants are built from complete local enumerations (every
    empty point in the region is tried), so the killing/living move sets
    are exhaustive within the region, every failing try becomes an
    annotated explanation line, and the settled variants are verified
    against every local challenge.  Quality gates reject mushy hinges.

    Returns (problems, reject_reason); problems is [] iff rejected."""
    color, attacker = h.color, opposite(h.color)
    board = h.base_board
    target = h.target
    to_move = "W" if h.base_moves[-1][0] == "B" else "B"
    region = bbox(target, size, cfg.margin)
    area = (region["x1"] - region["x0"] + 1) * (region["y1"] - region["y0"] + 1)
    # The region drives the enumeration cost (one deep query per empty
    # point), so an unbounded region is a hard reject — but only well past
    # what a legitimate large problem needs.  Everything below the hard cap
    # is allowed and merely costs quality points (see quality_score's
    # regionArea / groupSize terms), so roomy corner problems survive.
    if area > cfg.hard_max_region_area:
        return [], (f"region {area} > hard cap {cfg.hard_max_region_area} "
                    "(enumeration cost) — raise --hard-max-region-area to keep")
    if len(target) > cfg.hard_max_group:
        return [], (f"group {len(target)} > hard cap {cfg.hard_max_group} "
                    "stones — raise --hard-max-group to keep")

    # -------- exhaustive enumeration at the base, both sides ------------
    att = enumerate_tries(kg, h.base_moves, to_move, attacker, board, target,
                          size=size, rules=rules, komi=komi, visits=visits,
                          margin=cfg.margin,
                          top_n=getattr(cfg, 'policy_top', 5), log=log)
    dfn = enumerate_tries(kg, h.base_moves, to_move, color, board, target,
                          size=size, rules=rules, komi=komi, visits=visits,
                          margin=cfg.margin,
                          top_n=getattr(cfg, 'policy_top', 5), log=log)
    killing = [t for t in att if t["score"] <= -cfg.settle_thresh]
    living = [t for t in dfn if t["score"] >= cfg.settle_thresh]
    if not killing:
        return [], "no killing move in the region"
    if not living:
        return [], "no living move in the region"
    if len(killing) > cfg.max_solutions:
        return [], f"{len(killing)} killing moves — not sharp"
    if len(living) > cfg.max_solutions:
        return [], f"{len(living)} living moves — not sharp"

    ko_sol = any(t["ko"] for t in killing + living)
    if ko_sol and not cfg.allow_ko:
        return [], "solution lines contain a ko fight"

    # -------- connect / cut triviality gates ----------------------------
    # "big eyeless group just needs to connect or be cut off" is exactly
    # what these reject: a living move that links the group to an OUTSIDE
    # friendly group, or a killing move that works by severing one.
    if not getattr(cfg, "allow_connect", False):
        for t in living:
            if move_is_connect_out(board, t["move"], target, color, region):
                return [], (f"living move {t['move']} connects out of the "
                            "region — link-up, not eye-making")
        for t in killing:
            if move_is_cut(board, t["move"], target, color, region):
                return [], (f"killing move {t['move']} is a cut from "
                            "outside support — connect/cut, not a tsumego")

    # -------- how hidden is the vital point? ----------------------------
    # raw-policy prior of the solution moves at the base: if KataGo's
    # first glance already names the answer, the problem is obvious.
    pri = getattr(h, "priors", {}) or {}
    obvious = max([pri.get("att", {}).get(t["move"], 0.0) for t in killing]
                  + [pri.get("def", {}).get(t["move"], 0.0) for t in living]
                  + [0.0])

    # -------- eyespace cleanliness --------------------------------------
    space = getattr(h, "eyespace", set())
    purity, outlets = (eyespace_purity(board, space, target, color)
                       if space else (1.0, 0))

    # -------- locality: the fight must not swing the outside board ------
    best_kill = min(killing, key=lambda t: t["score"])
    best_live = max(living, key=lambda t: t["score"])
    outer = bbox(target, size, cfg.margin + 1)
    loc_delta = _mean_abs_delta_outside(best_kill["own"], best_live["own"],
                                        outer, size)
    if loc_delta > cfg.locality:
        return [], (f"not local: outside ownership swings by "
                    f"{loc_delta:.2f} between kill and live")

    # -------- surrounding wall must survive the group living ------------
    wall = [(x, y) for y in range(region["y0"], region["y1"] + 1)
            for x in range(region["x0"], region["x1"] + 1)
            if board.get(x, y) == attacker]
    wall_own = (group_mean_own(wall, best_live["own"], size, attacker)
                if wall else 1.0)

    ambiguity = (sum(1 for t in att + dfn if abs(t["score"]) < 0.5)
                 / max(1, len(att) + len(dfn)))
    edge_dist = min(region["x0"], region["y0"],
                    size - 1 - region["x1"], size - 1 - region["y1"])
    qual, qcomp = quality_score(
        n_kill=len(killing), n_live=len(living), area=area,
        max_area=cfg.max_region_area, ambiguity=ambiguity,
        loc_delta=loc_delta, loc_limit=cfg.locality, wall_own=wall_own,
        group_size=len(target), edge_dist=edge_dist,
        obvious=obvious, purity=purity, outlets=outlets,
        eyespace_size=len(space), soft_group=cfg.max_group)
    if qual < cfg.min_quality:
        return [], f"quality {qual} < {cfg.min_quality} ({qcomp})"

    meta0 = dict(h.meta)
    meta0.update({"quality": qual, "qualityComponents": qcomp,
                  "koSuspect": ko_sol, "solutionPrior": round(obvious, 3)})
    probs = []

    # -------- undecided ------------------------------------------------
    p = _base_json(board, size, rules, komi, color, target,
                   "undecided", attacker, prefix, dict(meta0))
    p["killing"] = {"toMove": attacker,
                    "moves": [t["move"] for t in killing],
                    "lines": [{"first": t["move"], "seq": t["seq"],
                               "groupScore": t["score"]} for t in killing]}
    p["living"] = {"toMove": color,
                   "moves": [t["move"] for t in living],
                   "lines": [{"first": t["move"], "seq": t["seq"],
                              "groupScore": t["score"]} for t in living]}
    fails = ([t for t in att if t["score"] > -cfg.settle_thresh]
             + [t for t in dfn if t["score"] < cfg.settle_thresh])
    p["explanationLines"] = _lines_of(fails, cfg.max_lines)
    probs.append(p)

    # -------- dead: base + most decisive killing move -------------------
    kill = best_kill["move"]
    dead_board = board.copy()
    dead_board.play(attacker, kill)
    dead_target = [(x, y) for (x, y) in target
                   if dead_board.get(x, y) == color]
    if len(dead_target) >= 3:
        dm = [m[:] for m in h.base_moves]
        if to_move != attacker:
            dm.append([to_move, "pass"])
        dm.append([attacker, kill])
        rescue = enumerate_tries(kg, dm, opposite(attacker), color,
                                 dead_board, dead_target, size=size,
                                 rules=rules, komi=komi, visits=visits,
                                 margin=cfg.margin,
                          top_n=getattr(cfg, 'policy_top', 5), log=log)
        bad = [t for t in rescue if t["score"] > -cfg.settled_check]
        ko_here = any(t["ko"] for t in rescue)
        if bad:
            if log:
                log(f"[{prefix}] dead variant skipped: "
                    f"{bad[0]['move']} keeps the group at "
                    f"{bad[0]['score']:+.2f}")
        elif ko_here and not cfg.allow_ko:
            if log:
                log(f"[{prefix}] dead variant skipped: refutations run "
                    "through a ko")
        else:
            meta = dict(meta0)
            meta.update({"settledBy": [attacker, kill], "koSuspect": ko_here})
            p = _base_json(dead_board, size, rules, komi, color, dead_target,
                           "dead", color, prefix, meta)
            p["lastMove"] = [attacker, kill]
            p["explanationLines"] = _lines_of(rescue, cfg.max_lines)
            probs.append(p)

    # -------- alive: base + most decisive living move -------------------
    live = best_live["move"]
    alive_board = board.copy()
    alive_board.play(color, live)
    alive_target = [(x, y) for (x, y) in target
                    if alive_board.get(x, y) == color]
    lx = gtp_to_xy(live, size)
    if lx and alive_board.get(*lx) == color:
        alive_target.append(lx)
    if len(alive_target) >= 3:
        am = [m[:] for m in h.base_moves]
        if to_move != color:
            am.append([to_move, "pass"])
        am.append([color, live])
        benson = alive_board.is_pass_alive(alive_target, color)
        attempts = enumerate_tries(kg, am, opposite(color), attacker,
                                   alive_board, alive_target, size=size,
                                   rules=rules, komi=komi, visits=visits,
                                   margin=cfg.margin,
                          top_n=getattr(cfg, 'policy_top', 5), log=log)
        bad = [t for t in attempts if t["score"] < cfg.settled_check]
        ko_here = (not benson) and any(t["ko"] for t in attempts)
        if bad and not benson:
            if log:
                log(f"[{prefix}] alive variant skipped: "
                    f"{bad[0]['move']} drops the group to "
                    f"{bad[0]['score']:+.2f}")
        elif ko_here and not cfg.allow_ko:
            if log:
                log(f"[{prefix}] alive variant skipped: some attack line "
                    "runs through a ko")
        else:
            meta = dict(meta0)
            meta.update({"settledBy": [color, live], "koSuspect": ko_here,
                         "bensonAlive": benson})
            if benson:
                meta["quality"] = min(100, meta["quality"] + 8)
            p = _base_json(alive_board, size, rules, komi, color,
                           alive_target, "alive", attacker, prefix, meta)
            p["lastMove"] = [color, live]
            lines = _lines_of(attempts, cfg.max_lines)
            if benson and not lines:
                lines = [{"seq": [], "groupScore": 1.0, "result": "alive",
                          "note": "pass-alive (Benson): every inside move "
                                  "is illegal — there is nothing to try"}]
            p["explanationLines"] = lines
            probs.append(p)

    return probs, None
