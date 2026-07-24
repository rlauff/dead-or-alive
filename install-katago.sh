#!/usr/bin/env bash
# One-shot KataGo setup for tsumego-factory.
#
#   ./install-katago.sh              # auto-pick a backend
#   ./install-katago.sh opencl       # force OpenCL (works on any NVIDIA/AMD
#                                    #   driver, needs no CUDA toolkit)
#   ./install-katago.sh cuda         # CUDA 12.x + cuDNN 8.9 build
#   ./install-katago.sh eigen        # CPU only (slow, but always works)
#
# Installs to ~/katago/ so that the generator's default paths work:
#   ~/katago/katago            the binary
#   ~/katago/kata-b18.bin.gz   the neural network
#
# Safe to re-run: existing good files are kept, broken ones replaced.
set -euo pipefail

KATAGO_VERSION="v1.16.5"
DEST="${KATAGO_DIR:-$HOME/katago}"
# Network download. katagotraining.org rewrites these paths from time to
# time, so several are tried and each is VERIFIED before being accepted;
# override with:  NET_URL=... ./install-katago.sh
NET_URLS=(
  "${NET_URL:-}"
  "https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b18c384nbt-latest.bin.gz"
  "https://media.katagotraining.org/g170/neuralnets/kata1-b18c384nbt-latest.bin.gz"
)
NET="$DEST/kata-b18.bin.gz"
BIN="$DEST/katago"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

for tool in curl unzip; do
    command -v "$tool" >/dev/null || die "'$tool' is required but not installed."
done

# ---------------------------------------------------------------- backend
backend="${1:-auto}"
if [ "$backend" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        # CUDA builds need the 12.x runtime; anything older must use OpenCL,
        # which talks to the driver directly and needs no toolkit at all.
        cuda_major="$(nvcc --version 2>/dev/null | sed -n 's/.*release \([0-9]*\).*/\1/p' || true)"
        if [ "${cuda_major:-0}" -ge 12 ] 2>/dev/null; then
            backend=cuda
        else
            backend=opencl
        fi
    else
        backend=eigen
    fi
    say "auto-detected backend: $backend"
fi

case "$backend" in
    opencl) ASSET="katago-$KATAGO_VERSION-opencl-linux-x64.zip" ;;
    cuda)   ASSET="katago-$KATAGO_VERSION-cuda12.5-cudnn8.9.7-linux-x64.zip" ;;
    eigen)  ASSET="katago-$KATAGO_VERSION-eigen-linux-x64.zip" ;;
    *)      die "unknown backend '$backend' (use opencl, cuda or eigen)" ;;
esac
BIN_URL="https://github.com/lightvector/KataGo/releases/download/$KATAGO_VERSION/$ASSET"

mkdir -p "$DEST"

# ---------------------------------------------------------------- binary
if [ -x "$BIN" ] && "$BIN" version >/dev/null 2>&1; then
    say "katago already present and runnable: $("$BIN" version | head -1)"
else
    say "downloading $ASSET"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --retry 3 -o "$tmp/kg.zip" "$BIN_URL" \
        || die "download failed: $BIN_URL"
    unzip -q -o "$tmp/kg.zip" -d "$tmp/x" || die "the archive is corrupt"
    found="$(find "$tmp/x" -type f -name katago | head -1)"
    [ -n "$found" ] || die "no 'katago' binary inside the archive"
    install -m 755 "$found" "$BIN"
    # KataGo ships a cacert next to the binary; keep it if present
    cac="$(find "$tmp/x" -type f -name 'cacert.pem' | head -1)"
    [ -n "$cac" ] && cp -f "$cac" "$DEST/" || true
    say "installed $BIN"
fi

"$BIN" version >/dev/null 2>&1 \
    || die "the binary will not run here — try: $0 opencl   (or: $0 eigen)"

# ---------------------------------------------------------------- network
net_ok() {
    [ -f "$NET" ] || return 1
    # a real network is ~100+ MB; a failed download is an HTML error page
    local sz; sz="$(stat -c %s "$NET" 2>/dev/null || stat -f %z "$NET")"
    [ "$sz" -gt 20000000 ] || return 1
    gzip -t "$NET" 2>/dev/null || return 1
}

if net_ok; then
    say "network already present: $NET ($(du -h "$NET" | cut -f1))"
else
    [ -f "$NET" ] && warn "existing $NET looks broken — re-downloading"
    say "downloading the b18 network (~100 MB)"
    for u in "${NET_URLS[@]}"; do
        [ -n "$u" ] || continue
        say "  trying $u"
        curl -fL --retry 2 -o "$NET" "$u" 2>/dev/null || continue
        net_ok && break
        warn "  that URL did not return a valid network"
    done
    net_ok || die "could not fetch a network automatically.

Download one by hand (any 'b18c384nbt' file) from
    https://katagotraining.org/networks/
then either save it as
    $NET
or re-run with the direct link:
    NET_URL='https://.../kata1-b18c384nbt-....bin.gz' $0 $backend

A valid file is ~100 MB or more. If yours is a few hundred bytes, the link
returned an error page rather than the network."
    say "installed $NET ($(du -h "$NET" | cut -f1))"
fi

# ---------------------------------------------------------------- smoke test
say "smoke-testing the analysis engine (first run may tune the GPU: minutes)"
cfg="$(dirname "$(readlink -f "$0")")/analysis.cfg"
[ -f "$cfg" ] || die "analysis.cfg not found next to this script"
probe='{"id":"t","moves":[],"rules":"chinese","komi":7.5,"boardXSize":13,"boardYSize":13,"maxVisits":2,"includeOwnership":true}'
if printf '%s\n' "$probe" | timeout 1800 "$BIN" analysis -model "$NET" -config "$cfg" 2>/dev/null | head -1 | grep -q '"id"'; then
    say "engine works."
else
    die "the engine did not answer. Run it by hand to see the error:
    $BIN analysis -model $NET -config $cfg"
fi

cat <<EOF

$(say "ready — this now works from the project directory:")

  python3 generator/generate.py \\
      --katago $BIN --model $NET \\
      --config analysis.cfg --size 13 --games 40000 \\
      --weak-visits 10 --selfplay-visits 100 --weak-side random \\
      --pass-probe --probe-every 3 \\
      --analysis-visits 800 --enum-visits 500 \\
      --max-solutions 8 --min-quality 0 --hard-open-eyespace 1000 \\
      --invade-prob 0.08

Candidates land in ./candidates/ ; review them with:

  python3 review_server.py      # then open http://localhost:8642/?review=1

EOF
