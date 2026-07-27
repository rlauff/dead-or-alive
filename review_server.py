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
import json
import shutil
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CANDIDATES = ROOT / "candidates"
ACCEPTED = ROOT / "accepted"
REJECTED = ROOT / "rejected"



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
    def do_GET(self):
        path = self.path.split("?")[0]
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
        if self.path.split("?")[0] != "/api/decision":
            return self._json({"error": "unknown endpoint"}, 404)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
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
        moved = transfer_manifest(name, "candidates",
                                  "accepted" if accept else None)
        n = rebuild_pool()
        return self._json({"ok": True, "poolSize": n, "manifested": moved})

    def log_message(self, fmt, *args):  # quieter logs
        if "/api/next" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args()
    for d in (CANDIDATES, ACCEPTED, REJECTED):
        d.mkdir(exist_ok=True)
    rebuild_pool()
    print(f"Review UI:  http://localhost:{args.port}/?review=1")
    print(f"Player UI:  http://localhost:{args.port}/")
    print(f"Folders:    candidates={CANDIDATES}  accepted={ACCEPTED}  "
          f"rejected={REJECTED}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
