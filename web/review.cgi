#!/usr/bin/python3
"""Review API as a single CGI script, for hosts where no daemon can run.

Layout expected (everything relative to THIS file, so it works in any
subdirectory of the web root):

    dead_or_alive/
        review.cgi          <- this file, chmod 755
        review-key.txt      <- sha256 hex of the shared secret, chmod 644
        index.html, app.js, ...
        manifest.json       <- players' index (accepted + finalized)
        manifest-candidates.json
        problems/           <- published problem JSON
        candidates/         <- awaiting review
        accepted/           <- accepted, live for players
        rejected/

There is no authentication: the review UI is simply not linked from the
page, so it is only reached by typing ?review on the end of the URL.  That
keeps it out of the way of ordinary visitors; it does NOT keep out anyone
who is actually looking.  Every decision is appended to review-log.jsonl.
"""
import json
import os
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATES = HERE / "candidates"
ACCEPTED = HERE / "accepted"
REJECTED = HERE / "rejected"
PUBLISHED = HERE / "problems"
LOG = HERE / "review-log.jsonl"


def reply(obj, status="200 OK"):
    body = json.dumps(obj).encode()
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)
    sys.exit(0)


def manifest_file(kind: str) -> Path:
    return HERE / ("manifest.json" if kind == "accepted"
                   else f"manifest-{kind}.json")


def load_manifest(path: Path):
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return list(d) if isinstance(d, list) else list(d.get("problems", []))


def save_manifest(path: Path, names):
    uniq = sorted(set(names))
    path.write_text(json.dumps({"problems": uniq, "count": len(uniq)},
                               indent=1))


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

    for d in (CANDIDATES, ACCEPTED, REJECTED, PUBLISHED):
        d.mkdir(exist_ok=True)

    if action == "stats":
        reply({"pending": len(list(CANDIDATES.glob("*.json"))),
               "accepted": len(list(ACCEPTED.glob("*.json"))),
               "rejected": len(list(REJECTED.glob("*.json")))})

    if action == "next":
        files = sorted(CANDIDATES.glob("*.json"))
        if not files:
            reply({"empty": True,
                   "accepted": len(list(ACCEPTED.glob("*.json")))})
        f = files[0]
        try:
            problem = json.loads(f.read_text())
        except json.JSONDecodeError:
            reply({"error": f"unreadable candidate {f.name}"},
                  "500 Internal Server Error")
        reply({"file": f.name, "problem": problem,
               "pending": len(files),
               "accepted": len(list(ACCEPTED.glob("*.json")))})

    if action == "decision":
        name = Path(str(data.get("file", ""))).name      # no path tricks
        src = CANDIDATES / name
        if not name.endswith(".json") or not src.exists():
            reply({"error": f"no such candidate {name}"}, "404 Not Found")
        accept = bool(data.get("accept"))
        shutil.move(str(src), str((ACCEPTED if accept else REJECTED) / name))

        # a problem is only listed once finalize.py has verified and
        # published it; accepting such a candidate just moves its entry
        cand_mf, acc_mf = manifest_file("candidates"), manifest_file("accepted")
        cand = load_manifest(cand_mf)
        moved = name in cand
        if moved:
            cand.remove(name)
            save_manifest(cand_mf, cand)
            if accept:
                acc = load_manifest(acc_mf)
                acc.append(name)
                save_manifest(acc_mf, acc)
            else:
                pub = PUBLISHED / name
                if pub.exists():
                    pub.unlink()

        with LOG.open("a") as fh:
            fh.write(json.dumps({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "file": name,
                "decision": "accept" if accept else "reject",
                "ip": os.environ.get("REMOTE_ADDR", "?"),
            }) + "\n")

        reply({"ok": True, "manifested": moved,
               "pending": len(list(CANDIDATES.glob("*.json"))),
               "accepted": len(list(ACCEPTED.glob("*.json")))})

    reply({"error": f"unknown action {action}"}, "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}"},
              "500 Internal Server Error")
