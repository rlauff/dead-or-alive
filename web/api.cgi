#!/usr/bin/python3
"""Review API as a single CGI script — no daemon, no ports.

Anyone holding the secret link can review:
    https://your.site/dead_or_alive/?key=THE_SECRET

Only a SHA-256 of the secret lives here, so this file is safe to commit.
Set it with:
    python3 -c "import hashlib,getpass; \
        print(hashlib.sha256(getpass.getpass('secret: ').encode()).hexdigest())"
and paste the result into SECRET_SHA256 below.

Problem data lives in ../dead_or_alive_data/ — OUTSIDE the deployed folder,
so `update.sh` (which deletes and re-copies the deployed folder) can never
wipe the review queue.
"""
import cgi
import hashlib
import hmac
import json
import os
import shutil
import sys
from pathlib import Path

# sha256 of the shared review secret — replace this
SECRET_SHA256 = "54610ebdef0e91e585a49e96a21df8cc83f5060d342f80178d7421a7e4a619cf"

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "dead_or_alive_data"
CANDIDATES, ACCEPTED, REJECTED = DATA / "candidates", DATA / "accepted", DATA / "rejected"
LOG = DATA / "review-log.jsonl"
PUB = HERE / "problems"


def reply(obj, status="200 OK"):
    body = json.dumps(obj).encode()
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(body)
    sys.exit(0)


def authorised(key: str) -> bool:
    if not key or len(SECRET_SHA256) != 64:
        return False
    return hmac.compare_digest(
        hashlib.sha256(key.encode()).hexdigest(), SECRET_SHA256)


def manifest(name):
    return HERE / ("manifest.json" if name == "accepted"
                   else f"manifest-{name}.json")


def load_manifest(path):
    try:
        d = json.loads(path.read_text())
    except Exception:                                    # noqa: BLE001
        return []
    return list(d.get("problems", [])) if isinstance(d, dict) else list(d)


def save_manifest(path, names):
    uniq = sorted(set(names))
    path.write_text(json.dumps({"problems": uniq, "count": len(uniq)}, indent=1))


def main():
    form = cgi.FieldStorage()
    action = form.getfirst("action", "")
    key = form.getfirst("key", "")

    if action == "check":
        return reply({"ok": authorised(key)})
    if not authorised(key):
        return reply({"error": "not authorised"}, "403 Forbidden")

    for d in (CANDIDATES, ACCEPTED, REJECTED):
        d.mkdir(parents=True, exist_ok=True)

    if action == "stats":
        return reply({"pending": len(list(CANDIDATES.glob("*.json"))),
                      "accepted": len(list(ACCEPTED.glob("*.json"))),
                      "rejected": len(list(REJECTED.glob("*.json")))})

    if action == "next":
        files = sorted(CANDIDATES.glob("*.json"))
        if not files:
            return reply({"empty": True,
                          "accepted": len(list(ACCEPTED.glob("*.json")))})
        p = json.loads(files[0].read_text())
        p["_file"] = files[0].name
        p["file"] = files[0].name
        p["pending"] = len(files)
        p["accepted"] = len(list(ACCEPTED.glob("*.json")))
        return reply(p)

    if action == "decision":
        name = Path(form.getfirst("file", "")).name        # no path tricks
        if not name.endswith(".json") or not (CANDIDATES / name).exists():
            return reply({"error": f"no such candidate {name}"}, "404 Not Found")
        accept = form.getfirst("accept", "") in ("1", "true", "True")
        dest = (ACCEPTED if accept else REJECTED) / name
        shutil.move(str(CANDIDATES / name), str(dest))

        # move the manifest entry with the file, exactly like review_server.py:
        # only problems that finalize.py has verified are listed, and an
        # accepted one becomes visible to players immediately
        cand_m, acc_m = manifest("candidates"), manifest("accepted")
        src = load_manifest(cand_m)
        moved = name in src
        if moved:
            src.remove(name)
            save_manifest(cand_m, src)
            if accept:
                save_manifest(acc_m, load_manifest(acc_m) + [name])
            else:
                (PUB / name).unlink(missing_ok=True)

        with LOG.open("a") as fh:
            fh.write(json.dumps({
                "time": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
                "remote": os.environ.get("REMOTE_ADDR", "?"),
                "file": name,
                "decision": "accept" if accept else "reject"}) + "\n")
        return reply({"ok": True, "manifested": moved,
                      "pending": len(list(CANDIDATES.glob("*.json")))})

    return reply({"error": "unknown action"}, "400 Bad Request")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                              # noqa: BLE001
        reply({"error": f"{type(exc).__name__}: {exc}"},
              "500 Internal Server Error")
