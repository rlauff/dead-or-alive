#!/usr/bin/env python3
"""Review server for generated problems.

Serves the same frontend as the final app, plus a small API that feeds it
candidate problems and records accept/reject decisions.  Run it in a second
terminal while generator/generate.py is producing candidates:

    python3 review_server.py --port 8642
    -> open http://localhost:8642/?review=1

Accepted problems are moved to accepted/ and web/problems.json is rebuilt
from that directory after every decision, so the final app is always in
sync.  Rejected candidates go to rejected/ (kept for post-mortems; delete
the directory if you don't care).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
import shutil
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CANDIDATES = ROOT / "candidates"
ACCEPTED = ROOT / "accepted"
REJECTED = ROOT / "rejected"



# ---------------------------------------------------------------------- auth
# Reviewers are configured OUTSIDE the code, one per line, in reviewers.txt:
#       name:salt:scrypt_hex
# generated with:  python3 review_server.py --add-reviewer NAME
# The file holds only salted hashes, never the password itself.
REVIEWERS = ROOT / "reviewers.txt"
REVIEW_LOG = ROOT / "review-log.jsonl"
_SESSIONS: dict[str, dict] = {}          # token -> {name, expires}
_FAILS: dict[str, list] = {}             # client ip -> recent failure times
SESSION_HOURS = 12


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                          n=16384, r=8, p=1, dklen=32).hex()


def add_reviewer(name: str, password: str) -> None:
    salt = secrets.token_hex(16)
    line = f"{name}:{salt}:{_hash(password, salt)}\n"
    with REVIEWERS.open("a") as fh:
        fh.write(line)
    REVIEWERS.chmod(0o600)


def check_password(name: str, password: str) -> bool:
    if not REVIEWERS.exists():
        return False
    for line in REVIEWERS.read_text().splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] != name:
            continue
        # constant-time compare so a wrong password cannot be timed out
        return hmac.compare_digest(_hash(password, parts[1]), parts[2])
    return False


def new_session(name: str) -> str:
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = {"name": name,
                      "expires": time.time() + SESSION_HOURS * 3600}
    return tok


def session_of(handler) -> dict | None:
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "tsumego_review":
            s = _SESSIONS.get(v)
            if s and s["expires"] > time.time():
                return s
            _SESSIONS.pop(v, None)
    return None


def log_decision(name: str, filename: str, accepted: bool, problem: dict):
    """Append-only record of who moved what, so the pool stays accountable."""
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "reviewer": name,
             "file": filename,
             "decision": "accept" if accepted else "reject",
             "status": problem.get("status"),
             "quality": (problem.get("meta") or {}).get("quality")}
    with REVIEW_LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def manifest_path(dirname: str) -> Path:
    """Same convention as finalize.py: accepted -> manifest.json, everything
    else -> manifest-<name>.json."""
    return WEB / ("manifest.json" if dirname == "accepted"
                  else f"manifest-{dirname}.json")


def _load_manifest(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(d, list):
        return [str(x) for x in d]
    return [str(x) for x in d.get("problems", [])]


def _save_manifest(path: Path, names) -> int:
    WEB.mkdir(exist_ok=True)
    uniq = sorted(set(names))
    path.write_text(json.dumps({"problems": uniq, "count": len(uniq)},
                               indent=1))
    return len(uniq)


def transfer_manifest(name: str, src_dir: str, dst_dir: str | None) -> bool:
    """Move a manifest entry when a problem is accepted or rejected.

    A problem is only listed in a manifest once finalize.py has verified and
    published it.  So the entry is transferred ONLY if it is present in the
    source manifest: accepting an un-finalized candidate does not add it to
    the accepted manifest (it has not been verified, and nothing is
    published for it yet).  Returns True if an entry was moved."""
    sp = manifest_path(src_dir)
    src = _load_manifest(sp)
    if name not in src:
        return False
    src.remove(name)
    _save_manifest(sp, src)
    if dst_dir is not None:
        dp = manifest_path(dst_dir)
        dst = _load_manifest(dp)
        dst.append(name)
        _save_manifest(dp, dst)
    else:
        # rejected: drop the published copy too
        pub = WEB / "problems" / name
        if pub.exists():
            pub.unlink()
    return True


def rebuild_pool():
    """Rebuild the inline fallback pool (problems.json) from accepted/.

    NOTE: this deliberately does NOT write manifest.json.  The manifests
    list only problems that finalize.py has verified and published; they are
    maintained by finalize.py and by transfer_manifest() on accept/reject.
    """
    probs = []
    for f in sorted(ACCEPTED.glob("*.json")):
        try:
            probs.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            print(f"[pool] skipping unreadable {f.name}")
    (WEB / "problems.json").write_text(json.dumps(probs))
    return len(probs)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ GET
    def _require_auth(self):
        s = session_of(self)
        if s is None:
            self._json({"error": "not authenticated"}, 401)
            return None
        return s

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/whoami":
            s = session_of(self)
            return self._json({"authenticated": s is not None,
                               "name": s["name"] if s else None})
        if path in ("/api/next", "/api/stats") and not self._require_auth():
            return
        if path == "/api/next":
            files = sorted(CANDIDATES.glob("*.json"))
            if not files:
                return self._json({"empty": True,
                                   "pending": 0,
                                   "accepted": len(list(ACCEPTED.glob('*.json')))})
            f = files[0]
            try:
                prob = json.loads(f.read_text())
            except json.JSONDecodeError:
                # generator may still be writing it; report as empty for now
                return self._json({"empty": True, "pending": len(files)})
            return self._json({"empty": False, "file": f.name, "problem": prob,
                               "pending": len(files),
                               "accepted": len(list(ACCEPTED.glob('*.json')))})
        if path == "/api/stats":
            return self._json({
                "pending": len(list(CANDIDATES.glob("*.json"))),
                "accepted": len(list(ACCEPTED.glob("*.json"))),
                "rejected": len(list(REJECTED.glob("*.json"))),
            })
        return super().do_GET()

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        route = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        if route == "/api/login":
            ip = self.client_address[0]
            recent = [t for t in _FAILS.get(ip, []) if time.time() - t < 300]
            _FAILS[ip] = recent
            if len(recent) >= 5:               # crude brute-force brake
                return self._json({"error": "too many attempts, wait 5 min"},
                                  429)
            name = str(data.get("name", ""))[:64]
            if check_password(name, str(data.get("password", ""))):
                tok = new_session(name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # HttpOnly: unreachable from page scripts, so an XSS bug
                # cannot steal the session
                self.send_header("Set-Cookie",
                                 f"tsumego_review={tok}; Path=/; HttpOnly; "
                                 f"SameSite=Strict; Max-Age={SESSION_HOURS*3600}")
                body = json.dumps({"ok": True, "name": name}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            _FAILS[ip] = recent + [time.time()]
            time.sleep(1.0)                    # slow down guessing
            return self._json({"error": "wrong name or password"}, 403)

        if route == "/api/logout":
            s = session_of(self)
            for tok, v in list(_SESSIONS.items()):
                if s and v is s:
                    _SESSIONS.pop(tok, None)
            return self._json({"ok": True})

        if route != "/api/decision":
            return self._json({"error": "unknown endpoint"}, 404)
        sess = self._require_auth()
        if not sess:
            return
        name = Path(str(data.get("file", ""))).name  # no path tricks
        src = CANDIDATES / name
        if not name.endswith(".json") or not src.exists():
            return self._json({"error": f"no such candidate {name}"}, 404)
        accept = bool(data.get("accept"))
        dest_dir = ACCEPTED if accept else REJECTED
        dest_dir.mkdir(exist_ok=True)
        shutil.move(str(src), str(dest_dir / name))
        # carry the finalized-and-published status across with the file: an
        # already-finalized candidate keeps its published copy and simply
        # moves manifest entry; an un-finalized one is not listed anywhere
        try:
            prob = json.loads((dest_dir / name).read_text())
        except Exception:                                   # noqa: BLE001
            prob = {}
        log_decision(sess["name"], name, accept, prob)
        moved = transfer_manifest(name, "candidates",
                                  "accepted" if accept else None)
        n = rebuild_pool()
        return self._json({"ok": True, "poolSize": n,
                           "manifested": moved,
                           "listed": len(_load_manifest(manifest_path("accepted")))})

    def log_message(self, fmt, *args):  # quieter logs
        if "/api/next" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep the default and put a TLS "
                         "reverse proxy in front for public deployment")
    ap.add_argument("--add-reviewer", metavar="NAME",
                    help="create a reviewer login and exit (prompts for the "
                         "password; only a salted hash is stored)")
    args = ap.parse_args()
    if args.add_reviewer:
        import getpass
        pw = getpass.getpass(f"password for {args.add_reviewer}: ")
        if len(pw) < 10:
            raise SystemExit("please use at least 10 characters")
        if pw != getpass.getpass("repeat: "):
            raise SystemExit("passwords do not match")
        add_reviewer(args.add_reviewer, pw)
        print(f"added reviewer {args.add_reviewer} to {REVIEWERS}")
        return
    if not REVIEWERS.exists():
        print("NOTE: no reviewers configured — the review UI is locked.\n"
              "      add one with:  python3 review_server.py --add-reviewer NAME")
    for d in (CANDIDATES, ACCEPTED, REJECTED):
        d.mkdir(exist_ok=True)
    rebuild_pool()
    print(f"Review UI:  http://localhost:{args.port}/?review=1")
    print(f"Player UI:  http://localhost:{args.port}/")
    print(f"Folders:    candidates={CANDIDATES}  accepted={ACCEPTED}  "
          f"rejected={REJECTED}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
