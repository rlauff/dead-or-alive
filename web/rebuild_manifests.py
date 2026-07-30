#!/usr/bin/env python3
"""Apply queued review decisions and rebuild the manifests from disk.

The live server does not let the CGI user move files between the pool
directories, so review.cgi only *records* decisions: accepting a problem
appends a line to review-log.jsonl and nothing else.  This script is the
other half.  Run it on the server as the user that owns the data:

    python3 rebuild_manifests.py                  apply the log, then reindex
    python3 rebuild_manifests.py --dry-run        report, change nothing
    python3 rebuild_manifests.py --rebuild-only   skip the log, just reindex
    python3 rebuild_manifests.py --apply-only     move files, leave manifests
    python3 rebuild_manifests.py --root DIR       default: this file's directory

Replaying the log is idempotent: an entry whose file already sits in the
right pool is skipped, so it is safe to run as often as you like -- from
cron, at the end of update.sh, or by hand after a review session -- and safe
to leave the log in place afterwards.

Unlike review.cgi, this script DOES list directories.  That is the whole
point: it runs as the owner, where listing works, and writes the result into
the manifests so that the web side never has to.
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


def apply_log(root, entries, dry_run):
    """Move each decided file into its destination pool. Idempotent."""
    stats = {"moved": 0, "already": 0, "missing": 0, "failed": 0}
    virtual = {}          # name -> pool, so a dry run tracks its own moves

    def locate(name):
        if name in virtual:
            return virtual[name]
        for pool in POOLS:
            try:
                if (root / pool / name).is_file():
                    return pool
            except OSError:
                continue
        return None

    for lineno, name, dest in entries:
        here = locate(name)
        if here is None:
            warn(f"line {lineno}: {name} is in no pool directory "
                 "(already applied and then deleted?)")
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


# ---------------------------------------------------------------- manifests
def scan(directory, verify):
    """Sorted *.json names actually present. Optionally parse each one."""
    try:
        names = sorted(p.name for p in directory.glob("*.json"))
    except OSError as exc:
        warn(f"cannot list {directory}: {exc}")
        return None, []
    if not verify:
        return names, []
    good, broken = [], []
    for name in names:
        try:
            json.loads((directory / name).read_text())
        except (OSError, ValueError) as exc:
            broken.append(name)
            warn(f"{directory.name}/{name} is not readable JSON ({exc}); "
                 "leaving it out of the manifest")
            continue
        good.append(name)
    return good, broken


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


def rebuild(root, web, dry_run, verify):
    ok = True
    total_broken = 0
    for pool in POOLS:
        directory = root / pool
        if not directory.is_dir():
            warn(f"{directory} does not exist; {MANIFEST[pool]} left alone")
            ok = False
            continue
        names, broken = scan(directory, verify)
        if names is None:
            ok = False
            continue
        total_broken += len(broken)
        path = web / MANIFEST[pool]
        state = write_manifest(path, names, dry_run)
        if state == "FAILED":
            ok = False
        print(f"  {MANIFEST[pool]:26s} {len(names):5d} problems  ({state})")
    return ok, total_broken


# --------------------------------------------------------------------- main
def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="Apply queued review decisions and rebuild the manifests.")
    ap.add_argument("--root", type=Path, default=here,
                    help="the dead-or-alive directory (default: %(default)s)")
    ap.add_argument("--web", type=Path, default=None,
                    help="where the manifests live (default: ROOT/web)")
    ap.add_argument("--log", type=Path, default=None,
                    help="decision log (default: ROOT/review-log.jsonl)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, change nothing")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="do not touch the log, only reindex what is on disk")
    ap.add_argument("--apply-only", action="store_true",
                    help="move the decided files but leave the manifests")
    ap.add_argument("--no-verify", action="store_true",
                    help="do not parse each problem file while reindexing")
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

    stats = None
    if not args.rebuild_only:
        entries, bad = read_log(log)
        if bad:
            warn(f"{bad} unparseable line(s) in {log.name}, ignored")
        if not log.exists():
            warn(f"{log} does not exist yet — no decisions to apply. "
                 "review.cgi needs to be able to create or append to it.")
        print(f"log  {log.name}: {len(entries)} decision(s) recorded")
        stats = apply_log(root, entries, args.dry_run)
        print(f"  applied {stats['moved']}, already in place "
              f"{stats['already']}, missing {stats['missing']}, "
              f"failed {stats['failed']}")

    ok, broken = True, 0
    if not args.apply_only:
        print("manifests:")
        ok, broken = rebuild(root, web, args.dry_run, not args.no_verify)

    problems = (not ok) or broken or (stats and stats["failed"])
    if problems:
        print("\nfinished with problems — see the messages above",
              file=sys.stderr)
        return 1
    print("\nup to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
