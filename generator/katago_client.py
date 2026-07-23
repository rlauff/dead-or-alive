"""Thin synchronous client for KataGo's JSON analysis engine.

Start KataGo once, send one query at a time, wait for the matching
response.  Also calibrates the perspective of the ownership array at
startup (it follows `reportAnalysisWinratesAs` in the analysis config:
BLACK, WHITE or SIDETOMOVE), so the rest of the code can always work with
ownership from Black's perspective (+1 = Black territory, -1 = White).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time


class KataGo:
    def __init__(self, katago_path: str, config_path: str, model_path: str,
                 extra_args: list[str] | None = None, log_stderr: bool = False):
        cmd = [katago_path, "analysis", "-config", config_path, "-model", model_path]
        if extra_args:
            cmd += extra_args
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._qid = 0
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []
        t = threading.Thread(target=self._drain_stderr, args=(log_stderr,), daemon=True)
        t.start()
        # Ownership perspective, one of "black", "white", "sidetomove";
        # None until calibrate() has run.
        self._own_mode: str | None = None

    def _drain_stderr(self, echo: bool):
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line.rstrip())
            if echo:
                print("[katago]", line.rstrip(), file=sys.stderr)

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    # ------------------------------------------------------------------ core
    def _build(self, moves, *, initial_stones=None, initial_player=None,
               rules="chinese", komi=7.5, size=19, max_visits=100,
               include_ownership=True, include_moves_ownership=False,
               include_policy=False, allow_moves=None) -> dict:
        """Build one query payload with a fresh id."""
        self._qid += 1
        q: dict = {
            "id": f"q{self._qid}",
            "moves": moves,
            "rules": rules,
            "komi": komi,
            "boardXSize": size,
            "boardYSize": size,
            "analyzeTurns": [len(moves)],
            "maxVisits": max_visits,
            "includeOwnership": include_ownership,
            "includeMovesOwnership": include_moves_ownership,
            "includePolicy": include_policy,
        }
        if initial_stones:
            q["initialStones"] = initial_stones
        if initial_player:
            q["initialPlayer"] = initial_player
        if allow_moves:
            # e.g. [{"player": "W", "moves": ["A1", "C1"], "untilDepth": 1}]
            q["allowMoves"] = allow_moves
        return q

    def _post(self, resp: dict, q: dict) -> dict:
        """Attach ownershipBlack (normalized so + = Black) to a response."""
        if q.get("includeOwnership") and "ownership" in resp:
            to_move = resp.get("rootInfo", {}).get("currentPlayer") \
                or self._to_move(q.get("moves", []), q.get("initialPlayer"))
            flip = self._flip_for(to_move)
            resp["ownershipBlack"] = [v * flip for v in resp["ownership"]]
            if q.get("includeMovesOwnership"):
                for mi in resp.get("moveInfos", []):
                    if "ownership" in mi:
                        mi["ownershipBlack"] = [v * flip for v in mi["ownership"]]
        return resp

    def query_many(self, specs: list[dict]) -> list:
        """Send several INDEPENDENT queries at once and collect all the
        responses.  KataGo's analysis engine interleaves them when
        `numAnalysisThreads` > 1, which keeps the NN batch fuller than one
        query at a time can.

        `specs` is a list of kwargs dicts for _build().  Returns a list
        aligned with `specs`; an element is the response dict, or a
        RuntimeError instance if THAT query failed (e.g. an illegal move),
        so one bad move never aborts the batch."""
        if not specs:
            return []
        if self._own_mode is None:
            self.calibrate()
        payloads = [self._build(**s) for s in specs]
        order = {q["id"]: i for i, q in enumerate(payloads)}
        out: list = [None] * len(payloads)
        with self._lock:
            assert self.proc.stdin and self.proc.stdout
            for q in payloads:
                self.proc.stdin.write(json.dumps(q) + "\n")
            self.proc.stdin.flush()
            remaining = len(payloads)
            while remaining:
                line = self.proc.stdout.readline()
                if not line:
                    tail = "\n".join(self._stderr_lines[-25:])
                    raise RuntimeError(f"KataGo terminated. Last stderr:\n{tail}")
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                i = order.get(resp.get("id"))
                if i is None:
                    continue                      # stale/unknown id
                if "warning" in resp and "moveInfos" not in resp \
                        and "error" not in resp:
                    continue                      # real result still follows
                if "error" in resp:
                    out[i] = RuntimeError(
                        f"KataGo error: {resp['error']} "
                        f"(field {resp.get('field')})")
                else:
                    out[i] = self._post(resp, payloads[i])
                remaining -= 1
        return out

    def query(self, moves: list[list[str]], *,
              initial_stones: list[list[str]] | None = None,
              initial_player: str | None = None,
              rules: str = "chinese", komi: float = 7.5, size: int = 19,
              max_visits: int = 100,
              include_ownership: bool = True,
              include_moves_ownership: bool = False,
              include_policy: bool = False,
              allow_moves: list[dict] | None = None,
              _skip_calibration: bool = False) -> dict:
        """Analyze the position after `moves`.  Returns the raw response,
        with response["ownershipBlack"] added (ownership normalized so that
        positive = Black) when ownership was requested; per-move ownership
        gets the same treatment as moveInfo["ownershipBlack"]."""
        q = self._build(
            moves, initial_stones=initial_stones, initial_player=initial_player,
            rules=rules, komi=komi, size=size, max_visits=max_visits,
            include_ownership=include_ownership,
            include_moves_ownership=include_moves_ownership,
            include_policy=include_policy, allow_moves=allow_moves)
        qid = q["id"]
        with self._lock:
            assert self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(json.dumps(q) + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    tail = "\n".join(self._stderr_lines[-25:])
                    raise RuntimeError(f"KataGo terminated. Last stderr:\n{tail}")
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                if resp.get("id") != qid:
                    continue                     # stale response for old query
                if "error" in resp:
                    raise RuntimeError(
                        f"KataGo error: {resp['error']} (field {resp.get('field')})")
                if "warning" in resp and "moveInfos" not in resp:
                    print(f"[katago warning] {resp['warning']} "
                          f"(field {resp.get('field')})", file=sys.stderr)
                    continue                     # real result still follows
                break

        if include_ownership and "ownership" in resp:
            if self._own_mode is None and not _skip_calibration:
                self.calibrate()
            to_move = resp.get("rootInfo", {}).get("currentPlayer") \
                or self._to_move(moves, initial_player)
            flip = self._flip_for(to_move)
            resp["ownershipBlack"] = [v * flip for v in resp["ownership"]]
            if include_moves_ownership:
                for mi in resp.get("moveInfos", []):
                    if "ownership" in mi:
                        # Per-move ownership uses the same reporting
                        # perspective as the root ownership.
                        mi["ownershipBlack"] = [v * flip for v in mi["ownership"]]
        return resp

    def _flip_for(self, to_move: str) -> float:
        mode = self._own_mode or "black"
        if mode == "black":
            return 1.0
        if mode == "white":
            return -1.0
        return 1.0 if to_move == "B" else -1.0   # sidetomove

    @staticmethod
    def _to_move(moves: list[list[str]], initial_player: str | None) -> str:
        if moves:
            return "W" if moves[-1][0] == "B" else "B"
        return initial_player or "B"

    # ------------------------------------------------- ownership calibration
    def calibrate(self):
        """Determine the reporting perspective of the ownership array
        (reportAnalysisWinratesAs = BLACK | WHITE | SIDETOMOVE).  We set up
        an overwhelmingly Black board and read the sign on a Black stone
        once with White to move and once with Black to move:

            perspective   White-to-move   Black-to-move
            BLACK              +               +
            SIDETOMOVE         -               +
            WHITE              -               -
        """
        if self._own_mode is not None:
            return
        stones = []
        for col in "CDEFGHJKLMNOPQR":
            for row in (3, 4, 5, 15, 16, 17):
                stones.append(["B", f"{col}{row}"])
        stones.append(["W", "A1"])

        def probe(player: str) -> float:
            resp = self.query([], initial_stones=stones, initial_player=player,
                              max_visits=8, include_ownership=True,
                              _skip_calibration=True)
            from board import gtp_to_xy
            x, y = gtp_to_xy("K4", 19)
            return resp["ownership"][y * 19 + x]

        v_w, v_b = probe("W"), probe("B")
        if v_w > 0 and v_b > 0:
            self._own_mode = "black"
        elif v_w < 0 and v_b > 0:
            self._own_mode = "sidetomove"
        elif v_w < 0 and v_b < 0:
            self._own_mode = "white"
        else:
            raise RuntimeError(
                f"ownership calibration failed (probes {v_w:+.2f}/{v_b:+.2f}) "
                "— board indexing or engine output is not what we expect")
        print(f"[calibrate] ownership perspective: {self._own_mode} "
              f"(probes W-to-move {v_w:+.2f}, B-to-move {v_b:+.2f})",
              file=sys.stderr)


def wait_ready(kg: KataGo, timeout: float = 300.0):
    """Block until KataGo answers a trivial query (model loaded, GPU tuned)."""
    t0 = time.time()
    kg.query([["B", "Q16"]], max_visits=2, include_ownership=False)
    print(f"[katago] ready in {time.time() - t0:.1f}s", file=sys.stderr)
