#!/usr/bin/python3
"""Review API as a single CGI script, for hosts where no daemon can run.

    dead-or-alive/
        web/
            review.cgi                <- this file, chmod 755
            index.html, app.js, ...
            manifest.json             <- players' index: the accepted pool
            manifest-candidates.json  <- the review queue
            manifest-rejected.json
        candidates/                   <- awaiting review
        accepted/                     <- accepted, live for players
        rejected/
        review-log.jsonl              <- decisions, appended here
        rebuild_manifests.py          <- carries them out, run by the owner

THIS SCRIPT NEVER WRITES ANYTHING EXCEPT ONE APPEND TO review-log.jsonl.
The live server does not let the CGI user move files between the pool
directories, so a decision is recorded, not performed: accepting a problem
appends a line to the log and nothing else.  Running rebuild_manifests.py on
the server (as the user that owns the data) replays the log, moves the files
and rewrites the manifests.  Until then the problem stays in candidates/ and
the review UI counts it under "to apply".

IT ALSO NEVER LISTS A DIRECTORY.  The pool directories can be entered but not
read (mode 711), which is why the manifests exist: each manifest IS the index,
and a problem whose name is not in one is unreachable by anybody.  Note that
`glob` on a traverse-only directory returns an empty list rather than raising,
so enumerating here fails silently and looks exactly like an empty queue.
Don't reintroduce it.  The consequence for the generator: putting a file in
candidates/ queues nothing, its name has to go into manifest-candidates.json.

There is no authentication: the review UI is simply not linked from the
page, so it is only reached by typing ?review on the end of the URL.  That
keeps it out of the way of ordinary visitors; it does NOT keep out anyone
who is actually looking.

?action=diag reports every resolved path and probes them by name.
"""
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../dead-or-alive/web
ROOT = HERE.parent                              # .../dead-or-alive

# pool -> (directory, manifest served to the browser)
POOLS = {
    "candidates": (ROOT / "candidates", HERE / "manifest-candidates.json"),
    "accepted": (ROOT / "accepted", HERE / "manifest.json"),
    "rejected": (ROOT / "rejected", HERE / "manifest-rejected.json"),
}
CANDIDATES, CAND_MF = POOLS["candidates"]
ACCEPTED, ACC_MF = POOLS["accepted"]
REJECTED, REJ_MF = POOLS["rejected"]
LOG = ROOT / "review-log.jsonl"

HINT = (f"candidates/ must be enterable at {CANDIDATES}, the manifests "
        f"readable in {HERE}, and {LOG.name} writable by the CGI user. "
        "Open review.cgi?action=diag for the resolved paths.")


def reply(obj, status="200 OK"):
    body = json.dumps(obj).encode()
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)
    sys.exit(0)


def load(path: Path):
    """The names a manifest lists, in order."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if isinstance(d, dict):
        d = d.get("problems", [])
    return [str(n) for n in d] if isinstance(d, list) else []


def decisions():
    """{name: 'accepted'|'rejected'} from the log; a later line wins."""
    out = {}
    try:
        text = LOG.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        name = Path(str(d.get("file", ""))).name
        verdict = str(d.get("decision", ""))
        if name.endswith(".json") and verdict in ("accept", "reject"):
            out[name] = "accepted" if verdict == "accept" else "rejected"
    return out


def split_queue():
    """(still to review, decided but not yet applied) from the manifest."""
    decided = decisions()
    listed = load(CAND_MF)
    pending = [n for n in listed if n not in decided]
    awaiting = [n for n in listed if n in decided]
    return pending, awaiting


def usable(directory: Path):
    """Can we enter this directory and open known names inside it?"""
    try:
        return directory.is_dir() and os.access(directory, os.X_OK)
    except OSError:
        return False


def log_writable():
    if LOG.exists():
        return os.access(LOG, os.W_OK)
    return os.access(ROOT, os.W_OK)


def counts(pending, awaiting, warnings):
    return {"pending": len(pending),
            "accepted": len(load(ACC_MF)),
            "rejected": len(load(REJ_MF)),
            "awaiting_apply": len(awaiting),
            "warnings": warnings}


def diagnostics():
    try:
        import pwd
        user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:                                     # noqa: BLE001
        user = str(os.geteuid())
    warnings = []
    pools = {}
    for name, (directory, mf) in POOLS.items():
        listed = load(mf)
        # probe by name: the only meaningful readability test when the
        # directory cannot be listed
        probe = {"name": None, "opens": None}
        if listed:
            probe["name"] = listed[0]
            try:
                (directory / listed[0]).read_bytes()
                probe["opens"] = True
            except OSError as exc:
                probe["opens"] = False
                probe["error"] = str(exc)
                warnings.append(f"{mf.name} lists {len(listed)} problem(s) but "
                                f"{directory / listed[0]} cannot be opened: {exc}")
        pools[name] = {
            "dir": str(directory),
            "is_dir": directory.is_dir(),
            "searchable": os.access(directory, os.X_OK),   # required
            "listable": os.access(directory, os.R_OK),     # not required
            "writable": os.access(directory, os.W_OK),     # not required
            "manifest": mf.name,
            "manifest_exists": mf.exists(),
            "manifest_lists": len(listed),
            "first_entry": probe,
        }
        if not usable(directory):
            warnings.append(f"{directory} is missing or cannot be entered")
    pending, awaiting = split_queue()
    if not log_writable():
        warnings.append(f"{LOG} is not writable by {user}: decisions cannot "
                        "be recorded at all")
    if awaiting:
        warnings.append(f"{len(awaiting)} decision(s) recorded but not yet "
                        "applied — run rebuild_manifests.py on the server")
    legacy = {}
    for p in (ROOT / "problems",
              ROOT.parent / "dead_or_alive" / "candidates",
              ROOT.parent / "dead_or_alive_data" / "candidates"):
        if p.exists():
            legacy[str(p)] = "exists — leftover from an older layout?"
    return {
        "script": str(Path(__file__).resolve()),
        "web_dir": str(HERE),
        "root": str(ROOT),
        "running_as": user,
        "enumerates_directories": False,
        "moves_files": False,
        "pools": pools,
        "log": {"path": str(LOG), "exists": LOG.exists(),
                "writable": log_writable(), "entries": len(decisions())},
        "queue": {"to_review": len(pending), "awaiting_apply": len(awaiting),
                  "first_awaiting": awaiting[0] if awaiting else None},
        "rebuild_script": {
            "path": str(ROOT / "rebuild_manifests.py"),
            "present": (ROOT / "rebuild_manifests.py").exists()},
        "legacy_paths": legacy,
        "warnings": warnings,
        "hint": HINT,
    }


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

    if action == "diag":
        reply(diagnostics())

    warnings = []
    if not usable(CANDIDATES):
        reply({"error": f"cannot enter the candidates directory: {CANDIDATES}",
               "hint": HINT}, "500 Internal Server Error")

    if action == "decision":
        name = Path(str(data.get("file", ""))).name      # no path tricks
        if not name.endswith(".json") or name not in load(CAND_MF):
            reply({"error": f"{name or '(none)'} is not in {CAND_MF.name}"},
                  "404 Not Found")
        accept = bool(data.get("accept"))
        # Record only. Moving the file is rebuild_manifests.py's job, because
        # the CGI user has no write access to the pool directories.
        try:
            with LOG.open("a") as fh:
                fh.write(json.dumps({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "file": name,
                    "decision": "accept" if accept else "reject",
                    "ip": os.environ.get("REMOTE_ADDR", "?"),
                }) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            reply({"error": f"could not record the decision in {LOG.name}: "
                            f"{exc}", "hint": HINT},
                  "500 Internal Server Error")
        pending, awaiting = split_queue()
        out = counts(pending, awaiting, warnings)
        out.update({"ok": True, "queued": True})
        reply(out)

    if action == "stats":
        pending, awaiting = split_queue()
        reply(counts(pending, awaiting, warnings))

    if action == "next":
        pending, awaiting = split_queue()
        if not log_writable():
            warnings.append(f"{LOG.name} is not writable — decisions cannot "
                            "be recorded")
        name, problem, gone = None, None, 0
        for candidate in pending:
            try:
                problem = json.loads((CANDIDATES / candidate).read_text())
                name = candidate
                break
            except (OSError, ValueError):
                gone += 1                    # listed but unreadable: skip it
        if gone:
            warnings.append(f"skipped {gone} queued name(s) that could not be "
                            f"read from {CANDIDATES}")
        if name is None:
            out = counts(pending, awaiting, warnings)
            out.update({"empty": True, "dir": str(CANDIDATES),
                        "manifest": CAND_MF.name})
            reply(out)
        out = counts(pending, awaiting, warnings)
        out.update({"file": name, "problem": problem})
        reply(out)

    reply({"error": f"unknown action {action}"}, "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}", "hint": HINT},
              "500 Internal Server Error")
