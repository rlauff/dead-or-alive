#!/usr/bin/python3
"""Review API as a single CGI script, for hosts where no daemon can run.

Layout expected (this file lives in web/; the problem data sits in sibling
directories one level up, so it works in any subdirectory of the web root):

    dead-or-alive/
        web/
            review.cgi                <- this file, chmod 755
            index.html, app.js, ...
            manifest.json             <- players' index (indexes accepted/)
            manifest-candidates.json  <- indexes candidates/
        candidates/                   <- awaiting review
        accepted/                     <- accepted, live for players
        rejected/
        review-log.jsonl

There is no authentication: the review UI is simply not linked from the
page, so it is only reached by typing ?review on the end of the URL.  That
keeps it out of the way of ordinary visitors; it does NOT keep out anyone
who is actually looking.  Every decision is appended to review-log.jsonl.

The manifests are pure directory indexes (the web server cannot list a
directory, so the player needs them).  They are rebuilt from the actual
directory contents on every request, which makes them self-healing: a stale
manifest published by update.sh is corrected as soon as this script runs.
"""
import json
import os
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../dead-or-alive/web
ROOT = HERE.parent                              # .../dead-or-alive
CANDIDATES = ROOT / "candidates"
ACCEPTED = ROOT / "accepted"
REJECTED = ROOT / "rejected"
LOG = ROOT / "review-log.jsonl"

# pool name -> (directory, manifest file served to the browser)
POOLS = {
    "accepted": (ACCEPTED, HERE / "manifest.json"),
    "candidates": (CANDIDATES, HERE / "manifest-candidates.json"),
    "rejected": (REJECTED, HERE / "manifest-rejected.json"),
}


def reply(obj, status="200 OK"):
    body = json.dumps(obj).encode()
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)
    sys.exit(0)


def names(directory: Path):
    return sorted(p.name for p in directory.glob("*.json"))


def sync_manifests():
    """Rewrite each manifest from its directory, but only when it changed."""
    for directory, path in POOLS.values():
        want = names(directory)
        body = json.dumps({"problems": want, "count": len(want)}, indent=1)
        try:
            if path.exists() and path.read_text() == body:
                continue
            path.write_text(body)
        except OSError:
            pass            # read-only deployment: serve what is on disk


def main():
    qs = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    action = (qs.get("action") or ["next"])[0]

    if os.environ.get("REQUEST_METHOD", "GET").upper() == "POST":
        try:
            n = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
            data = json.loads(sys.stdin.read(n) or "{}")
        except (ValueError, json.JSONDecodeError):
            reply({"error": "bad json"}, "400 Bad Request")
        action = "decision"
    else:
        data = {}

    for d in (CANDIDATES, ACCEPTED, REJECTED):
        try:
            d.mkdir(exist_ok=True)
        except OSError:
            pass

    if action == "decision":
        name = Path(str(data.get("file", ""))).name      # no path tricks
        src = CANDIDATES / name
        if not name.endswith(".json") or not src.exists():
            reply({"error": f"no such candidate {name}"}, "404 Not Found")
        accept = bool(data.get("accept"))
        shutil.move(str(src), str((ACCEPTED if accept else REJECTED) / name))

        with LOG.open("a") as fh:
            fh.write(json.dumps({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "file": name,
                "decision": "accept" if accept else "reject",
                "ip": os.environ.get("REMOTE_ADDR", "?"),
            }) + "\n")

    sync_manifests()

    if action in ("stats", "decision"):
        out = {"pending": len(names(CANDIDATES)),
               "accepted": len(names(ACCEPTED)),
               "rejected": len(names(REJECTED))}
        if action == "decision":
            out["ok"] = True
        reply(out)

    if action == "next":
        files = names(CANDIDATES)
        if not files:
            reply({"empty": True, "pending": 0,
                   "accepted": len(names(ACCEPTED))})
        f = CANDIDATES / files[0]
        try:
            problem = json.loads(f.read_text())
        except json.JSONDecodeError:
            reply({"error": f"unreadable candidate {f.name}"},
                  "500 Internal Server Error")
        reply({"file": f.name, "problem": problem,
               "pending": len(files),
               "accepted": len(names(ACCEPTED))})

    reply({"error": f"unknown action {action}"}, "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}"},
              "500 Internal Server Error")
