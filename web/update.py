#!/usr/bin/env python3
"""Apply queued review decisions and bring the manifests back in step.

The live server does not let the CGI user move files between the pool
directories, so review.cgi only *records* decisions: accepting a problem
appends a line to review-log.jsonl and nothing else.  This script is the
other half.  Run it on the server as the user that owns the data:

    python3 update.py                  apply the log, then reconcile
    python3 update.py --dry-run        report, change nothing
    python3 update.py --rebuild-only   skip the log, just reconcile
    python3 update.py --apply-only     move files, leave the manifests
    python3 update.py --root DIR       default: this file's directory

THE MANIFESTS ARE A WHITELIST AND THIS SCRIPT NEVER EXTENDS IT.  A problem
becomes visible only once finalize.py has verified it and written its name
into a manifest; raw generator output sitting in candidates/ is deliberately
unlisted and must stay that way, or players and reviewers would be shown
problems nothing has checked.  So the set of names in the three manifests, as
read at startup, is the only set this script will touch:

  * a decision for a name that is in no manifest is skipped, not applied;
  * a file on disk that is in no manifest is left alone and never added.

Within that set the script does three things: it replays the log, moving each
decided file into accepted/ or rejected/; it works out where every listed
file actually sits now and puts its name in the matching manifest; and it
drops a name whose file has vanished or no longer parses as JSON, since
listing it would only give the player a broken link.

Replaying the log is idempotent -- an entry whose file already sits in the
right pool is skipped -- so it is safe to run as often as you like, from
cron, at the end of a deploy, or by hand after a review session, and safe to
leave the log in place afterwards.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

POOLS = ("candidates", "accepted", "rejected")
MANIFEST = {"candidates": "manifest-candidates.json",
            "accepted": "manifest.json",
            "rejected": "manifest-rejected.json"}
VERDICT = {"accept": "accepted", "reject": "rejected"}


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


# ------------------------------------------------------------- the whitelist
def read_manifest(path):
    """The names a manifest lists, in order."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if isinstance(d, dict):
        d = d.get("problems", [])
    return [str(n) for n in d] if isinstance(d, list) else []


def whitelist(web):
    """Every name the manifests already list. Nothing else may be touched."""
    known = set()
    for pool in POOLS:
        known.update(read_manifest(web / MANIFEST[pool]))
    return known


def locate(root, name):
    """Which pool holds this file, by name -- no directory listing needed."""
    for pool in POOLS:
        try:
            if (root / pool / name).is_file():
                return pool
        except OSError:
            continue
    return None


# ------------------------------------------------------------------- the log
def read_log(path):
    """[(line number, filename, destination pool)] in the order recorded."""
    entries, bad = [], 0
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [], 0
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            bad += 1
            continue
        name = Path(str(d.get("file", ""))).name        # no path tricks
        dest = VERDICT.get(str(d.get("decision", "")))
        if not name.endswith(".json") or dest is None:
            bad += 1
            continue
        entries.append((lineno, name, dest))
    return entries, bad


def apply_log(root, entries, known, dry_run):
    """Move each decided, whitelisted file into its destination. Idempotent."""
    stats = {"moved": 0, "already": 0, "unlisted": 0, "missing": 0, "failed": 0}
    virtual = {}          # name -> pool, so a dry run tracks its own moves

    def where(name):
        return virtual.get(name) or locate(root, name)

    for lineno, name, dest in entries:
        if name not in known:
            warn(f"line {lineno}: {name} is in no manifest — not finalized, "
                 "left in place")
            stats["unlisted"] += 1
            continue
        here = where(name)
        if here is None:
            warn(f"line {lineno}: {name} is in no pool directory "
                 "(applied earlier and then deleted?)")
            stats["missing"] += 1
            continue
        if here == dest:
            stats["already"] += 1
            virtual[name] = dest
            continue
        if not (root / dest).is_dir():
            warn(f"line {lineno}: destination {root / dest} does not exist")
            stats["failed"] += 1
            continue
        print(f"  {'would move' if dry_run else 'moved'} {name}: "
              f"{here}/ -> {dest}/")
        if not dry_run:
            try:
                shutil.move(str(root / here / name), str(root / dest / name))
            except OSError as exc:
                warn(f"line {lineno}: could not move {name}: {exc}")
                stats["failed"] += 1
                continue
        virtual[name] = dest
        stats["moved"] += 1
    return stats


# ------------------------------------------------------------- the manifests
def write_manifest(path, names, dry_run):
    body = json.dumps({"problems": names, "count": len(names)}, indent=1)
    try:
        current = path.read_text()
    except OSError:
        current = None
    if current == body:
        return "unchanged"
    if dry_run:
        return "would write"
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(body)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        warn(f"cannot write {path}: {exc}")
        return "FAILED"
    return "written"


def reconcile(root, web, known, dry_run, verify):
    """Re-file every whitelisted name under the pool its file now sits in."""
    assigned = {pool: [] for pool in POOLS}
    missing, broken = [], []
    for name in sorted(known):
        pool = locate(root, name)
        if pool is None:
            missing.append(name)
            warn(f"{name} is listed but is in no pool directory; "
                 "dropping it from the manifest")
            continue
        if verify:
            try:
                json.loads((root / pool / name).read_text())
            except (OSError, ValueError) as exc:
                broken.append(name)
                warn(f"{pool}/{name} is not readable JSON ({exc}); "
                     "dropping it from the manifest")
                continue
        assigned[pool].append(name)

    ok = True
    for pool in POOLS:
        path = web / MANIFEST[pool]
        if not path.exists() and not assigned[pool]:
            continue                    # do not invent a manifest
        state = write_manifest(path, assigned[pool], dry_run)
        if state == "FAILED":
            ok = False
        print(f"  {MANIFEST[pool]:26s} {len(assigned[pool]):5d} problems  "
              f"({state})")
    return ok, missing, broken


def unlisted_on_disk(root, known):
    """How many files are waiting for finalize.py. Reporting only."""
    total = 0
    for pool in POOLS:
        try:
            names = {p.name for p in (root / pool).glob("*.json")}
        except OSError:
            return None
        total += len(names - known)
    return total


# --------------------------------------------------------------------- main
def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="Apply queued review decisions and reconcile the "
                    "manifests. Never adds a name no manifest listed before.")
    ap.add_argument("--root", type=Path, default=here,
                    help="the dead-or-alive directory (default: %(default)s)")
    ap.add_argument("--web", type=Path, default=None,
                    help="where the manifests live (default: ROOT/web)")
    ap.add_argument("--log", type=Path, default=None,
                    help="decision log (default: ROOT/review-log.jsonl)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, change nothing")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="do not touch the log, only reconcile the manifests")
    ap.add_argument("--apply-only", action="store_true",
                    help="move the decided files but leave the manifests")
    ap.add_argument("--no-verify", action="store_true",
                    help="do not parse each problem file while reconciling")
    args = ap.parse_args()

    root = args.root.resolve()
    web = (args.web or root / "web").resolve()
    log = (args.log or root / "review-log.jsonl").resolve()

    if not root.is_dir():
        sys.exit(f"error: no such directory: {root}")
    if not web.is_dir():
        sys.exit(f"error: no web directory: {web} (use --web)")

    print(f"root {root}")
    if args.dry_run:
        print("DRY RUN — nothing will be changed")

    known = whitelist(web)
    print(f"listed {len(known)} finalized problem(s); nothing outside that "
          "set will be added to a manifest")
    if not known:
        warn("the manifests list nothing at all — if that is unexpected, "
             "restore them before running this, because an empty whitelist "
             "means this script has nothing it is allowed to touch")

    stats = None
    if not args.rebuild_only:
        entries, bad = read_log(log)
        if not log.exists():
            warn(f"{log} does not exist yet — no decisions to apply. "
                 "review.cgi needs to be able to create or append to it.")
        if bad:
            warn(f"{bad} unparseable line(s) in {log.name}, ignored")
        print(f"log  {log.name}: {len(entries)} decision(s) recorded")
        stats = apply_log(root, entries, known, args.dry_run)
        print(f"  applied {stats['moved']}, already in place "
              f"{stats['already']}, unlisted {stats['unlisted']}, "
              f"missing {stats['missing']}, failed {stats['failed']}")

    ok, missing, broken = True, [], []
    if not args.apply_only:
        print("manifests:")
        ok, missing, broken = reconcile(root, web, known, args.dry_run,
                                       not args.no_verify)

    waiting = unlisted_on_disk(root, known)
    if waiting:
        print(f"note {waiting} file(s) on disk are in no manifest — waiting "
              "for finalize.py, left untouched")

    failed = stats["failed"] if stats else 0
    if not ok or missing or broken or failed:
        print("\nfinished with problems — see the messages above",
              file=sys.stderr)
        return 1
    print("\nup to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
