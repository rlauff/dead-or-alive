#!/usr/bin/python3
"""Review API as a single CGI script, for hosts where no daemon can run.

Layout expected (this file lives in web/; the problem data sits in sibling
directories one level up, so it works in any subdirectory of the web root):

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
        review-log.jsonl

NOTHING HERE LISTS A DIRECTORY.  This runs on a restricted server where the
pool directories can be entered but not read (mode 711 or similar), which is
the whole reason the manifests exist: they ARE the index, and a problem that
is not listed in one is not reachable by anybody, including this script.
Files are only ever opened by a name that came out of a manifest.

Note that `glob` on a traverse-only directory returns an empty list rather
than raising, so any code that enumerates here fails silently and looks
exactly like an empty queue.  Don't reintroduce it.  The consequence for the
generator: appending a file to candidates/ is not enough, its name has to go
into manifest-candidates.json or nothing will ever see it.

There is no authentication: the review UI is simply not linked from the
page, so it is only reached by typing ?review on the end of the URL.  That
keeps it out of the way of ordinary visitors; it does NOT keep out anyone
who is actually looking.  Every decision is appended to review-log.jsonl.

?action=diag reports every resolved path and probes them by name.
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

HINT = ("candidates/, accepted/ and rejected/ must sit beside web/, i.e. in "
        f"{ROOT}, and the manifests in {HERE} must be writable by the CGI. "
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


def save(path: Path, names, warnings):
    """Write a manifest atomically where possible, in place otherwise.

    os.replace needs write permission on web/; a host that only grants write
    on the files themselves still works, it just loses atomicity.
    """
    uniq = sorted(set(names))
    body = json.dumps({"problems": uniq, "count": len(uniq)}, indent=1)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(body)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        path.write_text(body)
        return True
    except OSError as exc:
        warnings.append(f"cannot write {path.name}: {exc}")
        return False


def usable(directory: Path):
    """Can we enter this directory and open known names inside it?"""
    try:
        return directory.is_dir() and os.access(directory, os.X_OK)
    except OSError:
        return False


def queue(warnings):
    """The pending candidates that actually have a file behind them.

    A name whose file has gone (decided out of band, or never delivered) is
    dropped from the manifest — the only way to keep it honest without
    listing the directory.
    """
    listed = load(CAND_MF)
    live, stale = [], []
    for name in listed:
        try:
            ok = (CANDIDATES / name).is_file()
        except OSError:
            ok = False
        (live if ok else stale).append(name)
    if stale:
        warnings.append(f"{len(stale)} queued name(s) have no file in "
                        f"{CANDIDATES} and were dropped from "
                        f"{CAND_MF.name} (first: {stale[0]})")
        save(CAND_MF, live, warnings)
    return live


def counts(pending=None):
    return {"pending": len(load(CAND_MF)) if pending is None else len(pending),
            "accepted": len(load(ACC_MF)),
            "rejected": len(load(REJ_MF))}


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
            "searchable": os.access(directory, os.X_OK),
            "writable": os.access(directory, os.W_OK),
            "listable": os.access(directory, os.R_OK),   # not required
            "manifest": mf.name,
            "manifest_exists": mf.exists(),
            "manifest_lists": len(listed),
            "manifest_writable": os.access(mf if mf.exists() else HERE, os.W_OK),
            "first_entry": probe,
        }
        if not usable(directory):
            warnings.append(f"{directory} is missing or cannot be entered")
    # legacy locations, probed by name — never enumerated
    legacy = {}
    for p in (ROOT / "problems",
              ROOT.parent / "dead_or_alive" / "candidates",
              ROOT.parent / "dead_or_alive_data" / "candidates",
              ROOT / "web" / "problems"):
        if p.exists():
            legacy[str(p)] = "exists — leftover from an older layout?"
    return {
        "script": str(Path(__file__).resolve()),
        "web_dir": str(HERE),
        "root": str(ROOT),
        "running_as": user,
        "enumerates_directories": False,
        "pools": pools,
        "log": {"path": str(LOG),
                "writable": os.access(LOG if LOG.exists() else ROOT, os.W_OK)},
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
        src = CANDIDATES / name
        if not src.is_file():
            reply({"error": f"queued but missing on disk: {src}"},
                  "404 Not Found")
        accept = bool(data.get("accept"))
        dest, dest_mf = (ACCEPTED, ACC_MF) if accept else (REJECTED, REJ_MF)
        if not usable(dest):
            reply({"error": f"cannot enter the destination directory: {dest}",
                   "hint": HINT}, "500 Internal Server Error")
        try:
            shutil.move(str(src), str(dest / name))
        except OSError as exc:
            reply({"error": f"could not move {name}: {exc}", "hint": HINT},
                  "500 Internal Server Error")

        # the file has moved; the manifests must follow or it becomes invisible
        ok = save(CAND_MF, [n for n in load(CAND_MF) if n != name], warnings)
        ok &= save(dest_mf, load(dest_mf) + [name], warnings)
        try:
            with LOG.open("a") as fh:
                fh.write(json.dumps({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "file": name,
                    "decision": "accept" if accept else "reject",
                    "ip": os.environ.get("REMOTE_ADDR", "?"),
                }) + "\n")
        except OSError as exc:
            warnings.append(f"cannot append to {LOG.name}: {exc}")
        out = counts()
        out.update({"ok": bool(ok), "warnings": warnings})
        if not ok:
            out["error"] = (f"{name} was moved into {dest.name}/ but the "
                            "manifests could not be updated")
            reply(out, "500 Internal Server Error")
        reply(out)

    if action == "stats":
        pending = queue(warnings)
        out = counts(pending)
        out["warnings"] = warnings
        reply(out)

    if action == "next":
        pending = queue(warnings)
        if not pending:
            out = counts(pending)
            out.update({"empty": True, "dir": str(CANDIDATES),
                        "manifest": CAND_MF.name, "warnings": warnings})
            reply(out)
        name = pending[0]
        try:
            problem = json.loads((CANDIDATES / name).read_text())
        except (OSError, ValueError) as exc:
            reply({"error": f"cannot read candidate {name}: {exc}",
                   "warnings": warnings}, "500 Internal Server Error")
        out = counts(pending)
        out.update({"file": name, "problem": problem, "warnings": warnings})
        reply(out)

    reply({"error": f"unknown action {action}"}, "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}", "hint": HINT},
              "500 Internal Server Error")
