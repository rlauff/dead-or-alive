"""Minimal Go board for the problem generator.

Coordinate system
-----------------
Internally a point is (x, y) with x = column 0..size-1 (left to right) and
y = row 0..size-1 counted FROM THE TOP.  This matches the ordering of
KataGo's ownership array ("reading order", top-left first), so
ownership[y * size + x] is the value at (x, y).

GTP coordinates ("A1".."T19", letter I skipped) have row 1 at the BOTTOM,
so gtp row r corresponds to y = size - r.
"""

from __future__ import annotations

GTP_COLS = "ABCDEFGHJKLMNOPQRST"  # no 'I'


def gtp_to_xy(move: str, size: int) -> tuple[int, int] | None:
    """'Q16' -> (x, y); returns None for 'pass'."""
    m = move.strip().upper()
    if m in ("PASS", ""):
        return None
    x = GTP_COLS.index(m[0])
    r = int(m[1:])
    return (x, size - r)


def xy_to_gtp(x: int, y: int, size: int) -> str:
    return f"{GTP_COLS[x]}{size - y}"


def opposite(color: str) -> str:
    return "W" if color == "B" else "B"


class Board:
    EMPTY = "."

    def __init__(self, size: int = 19):
        self.size = size
        self.grid: list[list[str]] = [[self.EMPTY] * size for _ in range(size)]
        self.captured = {"B": 0, "W": 0}  # stones captured *of* that color

    # ---------------------------------------------------------------- basics
    def get(self, x: int, y: int) -> str:
        return self.grid[y][x]

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def neighbors(self, x: int, y: int):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if self.in_bounds(nx, ny):
                yield nx, ny

    def copy(self) -> "Board":
        b = Board(self.size)
        b.grid = [row[:] for row in self.grid]
        b.captured = dict(self.captured)
        return b

    # ---------------------------------------------------------------- groups
    def group_at(self, x: int, y: int) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        """Return (stones, liberties) of the group containing (x, y)."""
        color = self.get(x, y)
        assert color != self.EMPTY
        stones, libs, stack, seen = set(), set(), [(x, y)], {(x, y)}
        while stack:
            px, py = stack.pop()
            stones.add((px, py))
            for nx, ny in self.neighbors(px, py):
                c = self.get(nx, ny)
                if c == self.EMPTY:
                    libs.add((nx, ny))
                elif c == color and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        return stones, libs

    def all_groups(self):
        """Yield (color, stones:set[(x,y)]) for every group on the board."""
        seen: set[tuple[int, int]] = set()
        for y in range(self.size):
            for x in range(self.size):
                c = self.get(x, y)
                if c != self.EMPTY and (x, y) not in seen:
                    stones, _ = self.group_at(x, y)
                    seen |= stones
                    yield c, stones

    # ---------------------------------------------------------------- moves
    def play(self, color: str, move: str) -> None:
        """Apply a GTP move ('Q16' or 'pass').  Assumes the move is legal
        (moves come from KataGo).  Handles captures; suicide is removed as a
        capture of the played group (KataGo never plays suicide under the
        default rules anyway)."""
        xy = gtp_to_xy(move, self.size)
        if xy is None:
            return
        x, y = xy
        assert self.get(x, y) == self.EMPTY, f"point {move} occupied"
        self.grid[y][x] = color
        opp = opposite(color)
        # remove opponent groups left without liberties
        for nx, ny in self.neighbors(x, y):
            if self.get(nx, ny) == opp:
                stones, libs = self.group_at(nx, ny)
                if not libs:
                    for sx, sy in stones:
                        self.grid[sy][sx] = self.EMPTY
                    self.captured[opp] += len(stones)
        # suicide safety net
        stones, libs = self.group_at(x, y)
        if not libs:
            for sx, sy in stones:
                self.grid[sy][sx] = self.EMPTY
            self.captured[color] += len(stones)

    # ---------------------------------------------------------------- export
    def stones_list(self) -> list[list[str]]:
        """[['B','Q16'], ...] — the whole position as setup stones."""
        out = []
        for y in range(self.size):
            for x in range(self.size):
                c = self.get(x, y)
                if c != self.EMPTY:
                    out.append([c, xy_to_gtp(x, y, self.size)])
        return out

    # ---------------------------------------------------------------- benson
    def pass_alive_chains(self, color: str) -> list[set[tuple[int, int]]]:
        """Benson's algorithm: the chains of `color` that are alive even if
        `color` passes forever (unconditional life — immune to ko, semeai,
        anything).  Returns the list of pass-alive chains (stone sets)."""
        # chains of `color` with liberties
        chains: list[tuple[frozenset, frozenset]] = []
        seen: set[tuple[int, int]] = set()
        for y in range(self.size):
            for x in range(self.size):
                if self.get(x, y) == color and (x, y) not in seen:
                    st, li = self.group_at(x, y)
                    seen |= st
                    chains.append((frozenset(st), frozenset(li)))
        # regions: connected components of non-`color` points
        region_id = {}
        regions: list[dict] = []
        for y in range(self.size):
            for x in range(self.size):
                if self.get(x, y) != color and (x, y) not in region_id:
                    stack, pts = [(x, y)], set()
                    region_id[(x, y)] = len(regions)
                    while stack:
                        px, py = stack.pop()
                        pts.add((px, py))
                        for nx, ny in self.neighbors(px, py):
                            if (self.get(nx, ny) != color
                                    and (nx, ny) not in region_id):
                                region_id[(nx, ny)] = len(regions)
                                stack.append((nx, ny))
                    empties = frozenset(p for p in pts
                                        if self.get(*p) == self.EMPTY)
                    # chains of `color` adjacent to this region
                    adj = set()
                    for (px, py) in pts:
                        for nx, ny in self.neighbors(px, py):
                            if self.get(nx, ny) == color:
                                for ci, (st, _) in enumerate(chains):
                                    if (nx, ny) in st:
                                        adj.add(ci)
                    regions.append({"empties": empties, "adj": frozenset(adj)})
        # iterate to the greatest fixpoint
        live = set(range(len(chains)))
        live_regions = set(range(len(regions)))
        while True:
            # region r is vital to chain c iff every empty point of r is a
            # liberty of c
            vital_count = {ci: 0 for ci in live}
            for ri in live_regions:
                r = regions[ri]
                for ci in r["adj"]:
                    if ci in live and r["empties"] <= chains[ci][1]:
                        vital_count[ci] += 1
            new_live = {ci for ci in live if vital_count[ci] >= 2}
            new_regions = {ri for ri in live_regions
                           if regions[ri]["adj"] <= new_live}
            if new_live == live and new_regions == live_regions:
                break
            live, live_regions = new_live, new_regions
        return [set(chains[ci][0]) for ci in live]

    def is_pass_alive(self, coords, color: str) -> bool:
        """True iff every stone in `coords` belongs to a pass-alive chain of
        `color` (Benson).  A pass-alive group cannot be killed even if its
        owner never responds — in particular its life cannot depend on ko."""
        pa = set()
        for ch in self.pass_alive_chains(color):
            pa |= ch
        return all((x, y) in pa for (x, y) in coords)
