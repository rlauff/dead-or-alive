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

The manifests are directory indexes (the web server cannot list a directory,
so the player needs them), rebuilt from the actual directory contents.  Two
rules keep that from doing damage when the paths are wrong:

  * the pool directories are NEVER created here.  A missing candidates/ is a
    deployment error, and creating an empty one would hide it.
  * a plain GET will not blank a non-empty manifest.  Emptiness is only
    trusted after a decision, which proves the directory is the right one by
    having just moved a file out of it.  Override with &force=1.

?action=diag reports every resolved path and what is actually there.
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

HINT = ("candidates/, accepted/ and rejected/ must sit beside web/, i.e. in "
        f"{ROOT}. Open review.cgi?action=diag for the resolved paths.")


def reply(obj, status="200 OK"):
    body = json.dumps(obj).encode()
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)
    sys.exit(0)


def listing(directory: Path):
    """Sorted *.json names, or None if that is not a readable directory."""
    try:
        if not directory.is_dir():
            return None
        return sorted(p.name for p in directory.glob("*.json"))
    except OSError:
        return None


def count(directory: Path):
    got = listing(directory)
    return None if got is None else len(got)


def manifest_names(path: Path):
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if isinstance(d, list):
        return list(d)
    if isinstance(d, dict):
        return list(d.get("problems", []))
    return []


def sync_manifests(trust=False):
    """Rewrite each manifest from its directory; return a list of warnings."""
    warnings = []
    for directory, path in POOLS.values():
        got = listing(directory)
        if got is None:
            warnings.append(f"{directory} does not exist or is unreadable; "
                            f"{path.name} left untouched")
            continue
        listed = manifest_names(path)
        if not got and listed and not trust:
            warnings.append(
                f"{directory} holds no problems but {path.name} lists "
                f"{len(listed)}; refusing to blank it. Fix the path, or add "
                f"&force=1 if the directory really is empty now")
            continue
        body = json.dumps({"problems": got, "count": len(got)}, indent=1)
        try:
            if not (path.exists() and path.read_text() == body):
                path.write_text(body)
        except OSError as exc:
            warnings.append(f"cannot write {path.name}: {exc}")
    return warnings


def entries(directory: Path, limit=60):
    try:
        got = sorted(p.name + "/" if p.is_dir() else p.name
                     for p in directory.iterdir())
    except OSError as exc:
        return f"unreadable: {exc}"
    return got[:limit] + ([f"... {len(got) - limit} more"]
                          if len(got) > limit else [])


def strays():
    """Queue directories left behind by an earlier layout, if any.

    An older version of this tool kept its data in the deployed folder
    itself, and an older one still in a sibling dead_or_alive_data/.  If the
    live queue is in one of those, this is where it shows up.
    """
    found = []
    for base in (ROOT, ROOT.parent):
        try:
            kids = sorted(base.iterdir())
        except OSError:
            continue
        for kid in kids:
            try:
                if not kid.is_dir() or "dead" not in kid.name.lower():
                    continue
            except OSError:
                continue
            for sub in ("candidates", "accepted", "rejected"):
                n = count(kid / sub)
                if n:
                    found.append({"dir": str(kid / sub), "json_files": n})
    return [f for f in found if f["dir"] not in
            (str(CANDIDATES), str(ACCEPTED), str(REJECTED))]


def diagnostics():
    try:
        import pwd
        user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:                                     # noqa: BLE001
        user = str(os.geteuid())
    pools = {}
    for name, (directory, path) in POOLS.items():
        got = listing(directory)
        pools[name] = {
            "dir": str(directory),
            "exists": directory.exists(),
            "readable": got is not None,
            "json_files": None if got is None else len(got),
            "writable": os.access(directory, os.W_OK),
            "manifest": path.name,
            "manifest_lists": len(manifest_names(path)),
        }
    return {
        "script": str(Path(__file__).resolve()),
        "web_dir": str(HERE),
        "root": str(ROOT),
        "running_as": user,
        "pools": pools,
        "beside_web": entries(ROOT),
        "manifests_writable": os.access(HERE, os.W_OK),
        "log_writable": os.access(LOG if LOG.exists() else ROOT, os.W_OK),
        "other_queues_found": strays(),
        "warnings": sync_manifests(),
        "hint": HINT,
    }


def main():
    qs = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    action = (qs.get("action") or ["next"])[0]
    force = (qs.get("force") or [""])[0] == "1"

    if os.environ.get("REQUEST_METHOD", "GET").upper() == "POST":
        try:
            n = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
            data = json.loads(sys.stdin.read(n) or "{}")
        except (ValueError, json.JSONDecodeError):
            reply({"error": "bad json"}, "400 Bad Request")
        action = "decision"
    else:
        data = {}

    if action == "diag":
        reply(diagnostics())

    if listing(CANDIDATES) is None:
        reply({"error": f"candidates directory not found: {CANDIDATES}",
               "hint": HINT, "warnings": sync_manifests()},
              "500 Internal Server Error")

    if action == "decision":
        name = Path(str(data.get("file", ""))).name      # no path tricks
        src = CANDIDATES / name
        if not name.endswith(".json") or not src.exists():
            reply({"error": f"no such candidate {name}"}, "404 Not Found")
        accept = bool(data.get("accept"))
        dest = ACCEPTED if accept else REJECTED
        if not dest.is_dir():
            reply({"error": f"destination directory not found: {dest}",
                   "hint": HINT}, "500 Internal Server Error")
        shutil.move(str(src), str(dest / name))
        force = True            # the move proves CANDIDATES is the right one

        with LOG.open("a") as fh:
            fh.write(json.dumps({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "file": name,
                "decision": "accept" if accept else "reject",
                "ip": os.environ.get("REMOTE_ADDR", "?"),
            }) + "\n")

    warnings = sync_manifests(trust=force)

    if action in ("stats", "decision"):
        out = {"pending": count(CANDIDATES), "accepted": count(ACCEPTED),
               "rejected": count(REJECTED), "warnings": warnings}
        if action == "decision":
            out["ok"] = True
        reply(out)

    if action == "next":
        files = listing(CANDIDATES)
        if not files:
            reply({"empty": True, "pending": 0, "accepted": count(ACCEPTED),
                   "dir": str(CANDIDATES), "warnings": warnings})
        f = CANDIDATES / files[0]
        try:
            problem = json.loads(f.read_text())
        except json.JSONDecodeError:
            reply({"error": f"unreadable candidate {f.name}"},
                  "500 Internal Server Error")
        reply({"file": f.name, "problem": problem, "pending": len(files),
               "accepted": count(ACCEPTED), "warnings": warnings})

    reply({"error": f"unknown action {action}", "warnings": warnings},
          "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}", "hint": HINT},
              "500 Internal Server Error")
