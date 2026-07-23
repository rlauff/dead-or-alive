# tsumego-factory

Generate life-and-death status problems from KataGo self-play, review them in
a browser, and serve the accepted pool as a fully static training app.

The pipeline: KataGo plays itself at low playouts while the generator watches
the ownership map. When a sizable group's ownership flips to ≥95% for the
opponent, the generator rewinds to just before the flip and re-analyzes deeply
in both directions — attacker to move (does a killing move exist?) and
defender to move, via an inserted pass (would there have been a living move?).
Only positions where **both** exist are genuine hinges. Each hinge yields up
to three problems:

| variant     | position                    | to move  | correct answer |
|-------------|-----------------------------|----------|----------------|
| `undecided` | the hinge itself            | attacker | Undecided — user then plays the killing move and the living move |
| `dead`      | hinge + best killing move   | defender | Unconditionally dead |
| `alive`     | hinge + best living move    | attacker | Unconditionally alive |

Main lines (KataGo principal variations; for the settled variants, the top
*local* failing tries — kill attempts against an alive group, rescue
attempts for a dead one — via a first-ply `allowMoves` restriction) are
stored inside each problem JSON, so the website needs
**no live engine** — everything is precomputed.

```
tsumego-factory/
├── analysis.cfg          KataGo analysis-engine config (edit paths/threads)
├── generator/
│   ├── generate.py       main entry point: self-play + detection + output
│   ├── detect.py         ownership-flip detection, validation, problem JSON
│   ├── katago_client.py  JSON analysis-engine wrapper (+ ownership calibration)
│   └── board.py          Go board with captures
├── review_server.py      serves the frontend + accept/reject API
├── finalize.py           deep re-verify + polish accepted problems
├── tools/mock_katago.py  fake engine for offline pipeline tests
├── candidates/           generator writes candidates here
├── accepted/ rejected/   review decisions land here
└── web/                  static frontend (player app = review app minus buttons)
    ├── index.html  app.js  goban.js  style.css
    └── problems.json     the accepted pool (rebuilt on every decision)
```

---

## Try it before installing anything

The repo ships with a working pool and queue so every part of the UI can be
exercised without KataGo:

- `accepted/` contains three hand-made sample problems (a straight three in
  the corner: its undecided, dead and alive variants) and `web/problems.json`
  is already built from them — start `python3 review_server.py` and open
  http://localhost:8642/ to play them.
- `candidates/` contains three machine-format candidates produced by a dry
  run, so http://localhost:8642/?review=1 immediately shows the review flow.
- `tools/mock_katago.py` is a fake analysis engine (scripted game + modelled
  ownership maps, *not* a Go engine) used to test the whole pipeline offline:

  ```bash
  python3 generator/generate.py --katago tools/mock_katago.py \
      --model fake --config fake --size 9 --games 1
  ```

  It should detect one ownership flip at move 14 and emit three problems.

## 0. Prerequisites

- Linux (instructions below; macOS/Windows work too, KataGo ships binaries
  for all three).
- A GPU is strongly recommended. CPU-only (Eigen backend) works but expect
  roughly 10–50× slower self-play.
- Python ≥ 3.10. **No third-party Python packages** — stdlib only.
- ~1 GB disk for KataGo + network.

GPU note for RTX 50-series (Blackwell): you need a **driver ≥ 570** and a
KataGo build against **CUDA 12.8+ / TensorRT 10.8+**, or simply the OpenCL
build, which has no CUDA version coupling and is the least fussy way to get
started. Start with OpenCL; switch to TensorRT later if you want ~1.5–2×
throughput.

## 1. Install KataGo

Grab the latest release from https://github.com/lightvector/KataGo/releases
(v1.16.x at the time of writing). Pick **one** archive:

- `katago-vX.Y.Z-opencl-linux-x64.zip` — easiest, works on any GPU vendor.
- `katago-vX.Y.Z-cuda12...-linux-x64.zip` — needs matching CUDA runtime.
- `katago-vX.Y.Z-trt10...-linux-x64.zip` — fastest, needs TensorRT installed.
- `katago-vX.Y.Z-eigen-linux-x64.zip` — CPU fallback.

```bash
mkdir -p ~/katago && cd ~/katago
# example: OpenCL build (check the releases page for the current version)
wget https://github.com/lightvector/KataGo/releases/download/v1.16.4/katago-v1.16.4-opencl-linux-x64.zip
unzip katago-v1.16.4-opencl-linux-x64.zip
chmod +x katago
./katago version        # should print version + backend
```

If `./katago version` complains about missing `libzip`/`OpenCL` libraries,
install them (`sudo apt install libzip5 ocl-icd-libopencl1` on Debian/Ubuntu,
plus your GPU vendor's OpenCL driver).

### NixOS instead

KataGo is in nixpkgs (`pkgs/by-name/ka/katago`, v1.16.5 on master as of
July 2026). The derivation takes
`backend ? if config.cudaSupport then "cuda" else "opencl"`, with the
allowed values `"opencl" | "cuda" | "tensorrt" | "eigen"` — so if your
system config already sets `nixpkgs.config.cudaSupport = true` you get the
CUDA build with no override at all. Either drop it into your system config
or use an ad-hoc shell:

```nix
# shell.nix
{ pkgs ? import <nixpkgs> { config.allowUnfree = true; } }:
pkgs.mkShell {
  packages = [
    (pkgs.katago.override { backend = "opencl"; })
    pkgs.python3
  ];
}
```

For CUDA/TensorRT on NixOS make sure `hardware.nvidia` gives you driver ≥ 570
for Blackwell, and expect a from-source build (cachix `cuda-maintainers` helps).
The prebuilt GitHub binary also runs fine under `steam-run` or
`nix-ld` if you'd rather not build.

## 2. Download a network

Networks live at https://katagotraining.org/networks/. The b18c384nbt line is
the best strength/speed compromise for this job (the bigger b28 nets are
stronger but ~2× slower, and we want *quantity* of games):

```bash
cd ~/katago
wget -O kata-b18.bin.gz \
  "https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz"
```

(Any recent `kata1-b18c384nbt-*.bin.gz` from that page is fine — just take the
latest one listed.)

## 3. Configure and smoke-test the analysis engine

This repo ships `analysis.cfg`. Tune two knobs if needed:
`numSearchThreads` (raise until the benchmark stops improving) and
`nnMaxBatchSize` (lower if you hit VRAM limits).

First run triggers GPU tuning (OpenCL) or engine building (TensorRT) — this
can take several minutes **once**; it's cached afterwards.

```bash
cd ~/katago
./katago benchmark -model kata-b18.bin.gz    # optional: find good thread count

# smoke test the analysis engine — paste one query, expect one JSON reply:
echo '{"id":"t","moves":[["B","Q16"]],"rules":"chinese","komi":7.5,"boardXSize":19,"boardYSize":19,"analyzeTurns":[1],"maxVisits":10,"includeOwnership":true}' \
  | ./katago analysis -config /path/to/tsumego-factory/analysis.cfg -model kata-b18.bin.gz
```

You should get a line of JSON containing `"moveInfos"` and a 361-float
`"ownership"` array. Ctrl-C to quit.

## 4. Generate problems

**Board size.** Smaller boards are much faster and produce compact groups,
which is exactly what you want for life-and-death — 9x9 or 13x13 is a good
default for volume, 19x19 for realistic corner/side shapes. Pass `--size`
and the generator picks a matching **preset** (`--preset auto`, the
default) that sets board-appropriate komi, game length, `min-group`,
invasion timing, and crucially `locality` — on small boards nearly every
point is "near" the fight, so the 19x19 locality bar (0.15) rejects
legitimate problems; the 9x9 preset loosens it to 0.45. Any knob you pass
explicitly overrides the preset. The chosen values are printed at startup
on the `[preset]` line.


```bash
cd tsumego-factory
python3 generator/generate.py \
    --katago ~/katago/katago \
    --model  ~/katago/kata-b18.bin.gz \
    --config analysis.cfg \
    --games 20 \
    --out candidates
```

Output is one line per game by default — timing, how many ownership flips
and pass-probe hits were examined, how many became hinges, and a bucketed
tally of why the rest were rejected (`screen 4, open 2, quality 1`) — plus
a line for every problem actually written. Use that tally to see which
gate dominates and which knob to loosen. `--verbose` restores the full
per-position detail including every enumerated move.

Historically you'll see per game: progress lines, then occasionally

```
[game 3] move 141: W group (9 stones) flipped -0.21 -> -0.98 around move 138; validating...
[game 3]   wrote g003m138_undecided_ab12cd34.json (undecided)
[game 3]   wrote g003m138_dead_ef56ab78.json (dead)
[game 3]   wrote g003m138_alive_90cd12ef.json (alive)
```

Knobs worth knowing (all `--help` documented):

- `--selfplay-visits 48` — visits per self-play move. Lower = faster games
  *and* more blunders, i.e. more groups actually dying. 24–64 is the sweet
  spot; at 48 visits a 19×19 game is a few minutes on a midrange GPU.
- `--analysis-visits 600` — depth of the validation/PV queries. Raise to
  1000+ if you see wrong "solutions" during review.
- `--min-group 5` — ignore small captures; raise for meatier problems.
- `--settle-thresh 0.90` — how decisively a candidate move must settle the
  group to count as killing/living.
- `--settled-check 0.70` — the `dead`/`alive` variants are only emitted if
  *every* well-visited reply in a deep re-check keeps the group settled
  beyond this |ownership| (otherwise the "unconditional" claim would be a
  lie and the variant is skipped with a log line). The re-check restricts
  the first move to the empty points near the group (KataGo `allowMoves`,
  first ply only — the search below is unrestricted), so it reads: *no
  local move changes the group's fate*. This also makes the stored
  explanation lines genuine local attempts — the attacker's failing kill
  tries for `alive`, the defender's failing rescue tries for `dead` —
  instead of whole-board tenuki/pass lines, which in settled positions
  used to render as one distant stone followed by only the opponent's dame
  fills. Lines are truncated at the first pass for the same reason. Known
  caveat: a kill/rescue depending on a *distant* first move (ladder
  breaker) is outside the local window.
- `--dead-thresh 0.95`, `--alive-before 0.5`, `--lookback 10` — the flip
  detector: "was above −0.5 within the last 10 moves, is below −0.95 now".
- `--seed 1` for reproducible runs.

Expect very roughly 0.5–2 hinges per game; not every game has a big group die
cleanly. If nothing comes out after a few games, lower `--min-group` to 4 and
`--alive-before` to 0.3.

## 5. Review while it generates

Second terminal:

```bash
cd tsumego-factory
python3 review_server.py --port 8642
```

Open **http://localhost:8642/?review=1**. Candidates appear as the generator
drops them (the page polls). For each one you get the exact same UI as the
final app — judge it, play the killing/living move, step through the stored
lines, free-play on the board — plus a metadata panel (ownership flip scores,
solution moves, visits) and the two extra buttons:

- **Accept** (key `a`) → moves the file to `accepted/`, rebuilds
  `web/problems.json`.
- **Reject** (key `r`) → moves it to `rejected/`.

Review advice: check that the "killing"/"living" first moves actually make
sense to a human, and that the main line doesn't depend on an unresolved ko —
NN "unconditional" life/death is statistical best-play judgment, not Benson's
algorithm, so ko-for-life positions can slip through and are best rejected
(or kept deliberately, your call) — §6 explains the Benson certificates
and ko flags that automate exactly this concern.

## 6. Built-in exhaustive validation & quality gates

Everything that used to be a separate post-processing step is part of
generation itself: a problem only ever reaches `candidates/` after passing
the full battery, so whatever you accept is final.

Per detected ownership flip the generator runs two stages. A cheap
**screen** (2 queries) drops events with no plausible killing+living pair.
Survivors get the **exhaustive enumeration**: every empty point in the
region (group bbox + `--margin`) is tried as a first move for each side,
one deep query each (`--enum-visits`, default `--analysis-visits`), so the
killing/living move sets are complete within the region, every failing try
becomes an annotated explanation line, and the dead/alive variants are
verified against every local challenge. All lines are trimmed at the first
pass and at the first move leaving the region — they always concern the
group.

### Where the difficulty comes from

Ownership flips alone are dominated by trivia: obvious captures, cut-off
tails, big eyeless groups that just need to connect. Two mechanisms fix
this.

**Asymmetric self-play (the main engine for dead groups).** Pass
`--weak-visits N` (e.g. 20) to make one side search only N visits while the
other uses `--selfplay-visits` (e.g. 250). The strong side systematically
out-reads the weak side and kills its groups in clean, readable ways —
this is by far the most productive source of genuine life-and-death
positions. `--weak-side B|W|random` picks which colour is weak (default
random per game). In this mode only the weak side invades (its group is
the one that will be attacked).

**Forced invasions (sparingly).** With probability `--invade-prob`
(default 0.05, subject to `--invade-cooldown` moves between invasions,
between `--invade-after` and `--invade-until`) the mover plays a plausible
invasion deep in the opponent's sphere (ownership ≤ −0.55 against the
mover, off the first line), sampled by KataGo's raw policy so it is a move
a player might try — a 3-3 under a star point, a shoulder hit, an
attachment. The opponent walls it in and the invader must live by making
eyes. The cooldown matters: without it, back-to-back invasions never let a
group settle into a readable shape (and nothing validates). A few
invasions per game is plenty. Normal move sampling is additionally biased
toward contested areas (`--no-contested-bias` to disable).

**Finding the hinge.** A group's death is usually sealed a move or two
before its ownership actually flips (the killing move takes the vital
point; the flip registers only once the capture resolves). The generator
therefore walks `--rewind` positions (default 4) back from the observed
death and takes the first that still screens as a genuine hinge — the last
moment the group was savable. If real games clearly contain killable
groups but almost everything is rejected at the screen with "no plausible
living move", increase `--rewind`.

**Shape gates** (free board checks, applied before any deep analysis):

- `--min-liberties 3` — a group at 1-2 liberties is an obvious capture,
  not a problem.
- `--policy-top 5` (finalize.py): at every node, besides the local points,
  also try the engine's N highest-visit moves **however far from the group
  they are**. Distance alone is a bad filter — a vital point can sit well
  outside the neighbourhood (a distant eyespace-reducing placement, a
  ladder breaker, an approach that removes a liberty), and those are
  exactly the moves a strong net spends its visits on. The stored region is
  widened to cover whatever gets tried, so a distant move's refutation is
  not trimmed away. `0` restores local-only behaviour.
- `--min-eyespace 3` rejects 1-2 point eyespaces (snapback / single
  connect-cut). The upper bound is now a HARD cap only,
  `--hard-open-eyespace 30`: a group whose reachable empty space exceeds
  that is genuinely OPEN (it runs to the center) and is dropped as a
  middlegame fight. Everything below the cap — including roomy shapes like
  rectangular six, comb formations and large corner invasions — passes;
  size only nudges the quality score (4-8 points gets a small bonus,
  larger is neutral, not penalized).
- connect/cut detectors — a living move that merges the group with a
  friendly group reaching outside the region ("just connect"), or a
  killing move adjacent to such a group ("just cut"), is rejected
  (`--allow-connect` keeps them). This removes exactly the big-eyeless-
  group-links-up-or-dies category.
- **obviousness** — the raw-policy prior of the solution moves is stored
  (`meta.solutionPrior`) and penalized in the quality score: if KataGo's
  first glance already names the vital point, the problem is easy; a
  solution only deep search finds is a good one. The eyespace size also
  feeds the score (4-8 point spaces — bent four, rectangular six — are
  the sweet spot), as does enclosure purity (an eyespace bordered by
  anything except the attacker's wall and the edge leaks connections).

Quality gates, each with a knob and a logged rejection reason:

- `--max-solutions 3` — more killing (or living) moves than this means
  "anything works": rejected.
- `--max-region-area 56`, `--max-group 16` are now SOFT thresholds: past
  them a problem loses a few quality points but still passes. The actual
  rejects are the high hard caps `--hard-max-region-area 140` and
  `--hard-max-group 40`, which exist mainly to bound enumeration cost (one
  deep query per empty point in the region). Large-but-legitimate corner
  problems survive; only genuine dragons/semeai are cut.
- `--locality 0.15` — the mean |ownership| swing OUTSIDE the region
  between the kill and the live branch must stay below this; otherwise the
  fight entangles the whole board. On 9x9 raise it (0.3–0.5): small-board
  fights are never local by this measure.
- `--min-quality 40` — a 0–100 score assembled from solution sharpness,
  region compactness, ambiguity (murky local moves), locality, wall
  survival (the surrounding stones must stay alive when the group lives —
  otherwise it is a capturing race) and an edge/corner bonus. The score
  and its components are stored in `meta` and shown in the review panel,
  so you can see WHY a problem scored what it did and tune the knobs.
- ko: solutions or refutations running through a ko recapture are rejected
  by default (`--allow-ko` keeps them, flagged `koSuspect`).

Every rejection prints its reason — after a batch, skim the log to see
which gate dominates and adjust. Expect a much lower but much better
yield than the old pipeline: on 19x19 with a strong net, roughly one
accepted-grade hinge every few games.

### Turn-agnosticism, ko, and what "unconditional" means here

The judging question is deliberately turn-free: the verdict must hold no
matter who moves first. For `undecided` both directions are verified by
construction; for the settled variants the non-trivial direction is
verified exhaustively over the region.

Ko is the classic hole in engine-scored "unconditional" claims. Two layers
address it:

- **Benson certificates** (`meta.bensonAlive`): every `alive` variant is
  checked with Benson's pass-alive algorithm. A pass-alive group is
  *provably* unconditionally alive — immune to ko, semeai, everything —
  no engine involved. Shown as "pass-alive ✓"; it also overrides engine
  noise in the settled check.
- **Ko detection** (`meta.koSuspect`): every stored line is replayed and
  scanned for ko recaptures. Flagged problems show "⚠ ko" in the plate
  and review panel and are rejected by default (see `--allow-ko`).

There is no Benson-style dual certificate for dead groups, so `dead`
problems rely on the exhaustive local check plus the ko flag.

## 7. The solving flow

The initial presentation is turn-free: no side to move is shown or needed —
you judge the marked group as unconditionally alive, dead, or undecided.

For **undecided** problems the claim then becomes concrete: "White to play
— kill the marked group" (a translucent ghost stone of the side to play
follows the mouse), then the same for the living move. A correct move
plays out its main line. A WRONG move that the generator enumerated plays
out too — your stone appears and the precomputed refutation answers it
(try a false living move and watch the killing response), with the ◀ ▶
stepper available and a "Try again" button. Only moves outside the
enumerated region get a plain "not in the precomputed lines" marker.

For **settled** problems the board goes interactive immediately after
judging: the challenging side gets the ghost stone ("White to try — kill
the marked group"), and every click that matches a precomputed attempt
plays out its refutation. Since the enumeration is exhaustive over the
region, essentially every sensible attempt is covered.

The line list under the board is the same book: solution lines and every
enumerated failing try, all clickable, all steppable.


## 7b. Finalizing accepted problems: `finalize.py`

Generation is tuned for throughput, so its lines are shallow and its group
marks and cropping are approximate. Once you have curated a batch in
`accepted/`, run the finalizer to turn each survivor into a polished,
deeply-verified problem:

```bash
python3 finalize.py \
    --katago ~/katago/katago \
    --model  ~/katago/kata-b18.bin.gz \
    --config analysis.cfg \
    --visits 5000 --deep-plies 24
```

At 5000 visits per query it, for every problem in `accepted/`:

- **Re-verifies the claim from scratch.** Undecided must still have a
  killing move (attacker first) AND a living move (defender first); dead
  must stay dead against every local defence; alive against every local
  attack. Anything that fails at high visits is **deleted** — a generation
  false-positive from a low-visit playout does not survive.
- **Recomputes the marked group** from the recorded target points (the
  actual connected group on the board), fixing an over-marked group (an
  extra wall stone) or an under-marked one (a missing stone). If the marks
  no longer identify a single group the problem is deleted.
- **Removes cropping** (`region` is dropped) so the whole board shows —
  13x13 and smaller are perfectly readable, and a tight crop hides
  ladders, approach moves and outside liberties that change the answer.
- **Drops the last-move dot** (`lastMove`): only the group under
  investigation is marked, so nothing gives the answer away.
- **Deepens the solution and adds a WEAK SOLUTION.** Each confirmed
  solution move keeps a deep principal variation (up to `--deep-plies`,
  extended while the fight stays local). In addition, every half-way
  sensible first move the challenger might try is stored with a single
  refuting reply, so in the UI clicking *any* plausible wrong move plays
  out why it fails. The solution set is thus confirmed by exhaustion, not
  by trusting one line, and the board's explore mode becomes dense.

**Which directories.** `--dir` takes one or more sources, so you can
finalize the reviewed pool, the unreviewed queue, or both in one run:

```bash
python3 finalize.py ... --dir accepted candidates
```

Each source gets its **own** manifest, so unreviewed problems never leak
into the player's default pool: `accepted/` -> `web/manifest.json` (loaded
by default), `candidates/` -> `web/manifest-candidates.json` (loaded with
`?pool=candidates`), anything else -> `manifest-<name>.json`. All
published problem files share `web/problems/`. This is what lets you
review on a restricted static server, which has no `/api/next`: finalize
`candidates/` and open `index.html?pool=candidates`.

**A manifest entry means "verified and published", and it follows the
file.** Finalizing is expensive, so it happens once:

- finalize **skips** any problem already listed in its folder's manifest
  (`--force` re-does it), so re-running over a folder only processes what
  is new;
- when you **accept** a candidate that was already finalized, the review
  server moves its entry from `manifest-candidates.json` into
  `manifest.json` — the published file is untouched and nothing is
  recomputed;
- accepting a candidate that was **not** finalized adds nothing to
  `manifest.json`: it has not been verified and nothing is published for
  it. Run finalize over `accepted/` afterwards to pick it up;
- **rejecting** drops the entry from `manifest-candidates.json` and
  deletes the published copy.

So `manifest.json` is always exactly "the accepted problems that have
passed deep verification", and `web/problems.json` (rebuilt by the review
server from every accepted file) remains the un-verified inline fallback
used only when no manifest is present.

**Publishing + manifest.** Each finalized problem is written to
`web/problems/<name>.json` and its filename appended to its manifest — written after *every* problem, so a crash mid-batch
still leaves a manifest matching what is actually published. The player
loads `manifest.json` first and then fetches problems one at a time by
name, which is what makes deployment to a **restricted/static server
possible: nothing needs to list a directory**, and the initial page load
stays small even though finalized problems can reach ~1 MB each. (If no
manifest is present the player falls back to the inline `problems.json`
that `review_server.py` builds.) A deleted problem is removed from both
the manifest and `web/problems/`.

**Weak-solution depth.** `--weak-depth 3` (default) means the challenger
may try, be refuted, try again, be refuted, and try once more before
giving up — so a player can keep probing until convinced, not just see a
single reply. `--weak-branch 3` bounds the tree by continuing only the
most tempting failures at each level, and `--weak-visits 1500` uses a
lower budget for this dialogue than the `--visits 5000` used for the
verification and the main lines. Cost grows roughly as
`points x branch^(depth-1)`, so raise depth before branch.

Files are rewritten in place and may grow to ~1 MB (that is fine).

**Throughput.** Queries are pipelined: every candidate first move in a
position goes to KataGo as one batch, and `--analysis-threads N`
(default 8) overrides `numAnalysisThreads` so the engine searches N of
them at once. A single query with `numSearchThreads = 16` only fills half
of `nnMaxBatchSize = 32`, so on a GPU the fuller batches raise throughput;
on a CPU backend (already compute-bound) it makes no difference. Note that
`nvidia-smi` showing 100% does NOT mean the GPU is saturated — it reports
the fraction of time a kernel is resident, not batch occupancy. A/B it on
your own machine with `--analysis-threads 1` vs `8`; if the times match,
you are genuinely compute-bound and should buy depth by lowering
`--weak-visits` instead. Raising `nnMaxBatchSize` in `analysis.cfg` helps
if higher thread counts do pay off.

Output is one line per problem — `[name] status: ok — N lines, K KB` or
`[name] status: DELETED — reason` — with already-finalized files reported
as a single per-directory count. `--verbose` prints every enumerated move
and readout; `--quiet` prints only the closing summary. Use `--keep-going` to skip past
a file that errors rather than aborting the batch, and `--settled-check`
to set how decisively every challenger reply must fail for a dead/alive
claim to stand.

## 8. Play / deploy

The trainer itself is 100% static — `web/` plus `problems.json` — so:

```bash
# locally, either reuse the review server without ?review=1, or:
cd web && python3 -m http.server 8000     # http://localhost:8000
```

To publish, copy `web/` anywhere that serves static files (nginx, GitHub
Pages, …). No backend, no engine, no build step.

Player UI flow: judge the marked group with the three buttons. If the truth is
*undecided*, you're immediately prompted to play the killing move, then the
living move; wrong tries are marked, two misses unlock **Hint** and **Show
solution**. Every problem has an **Explore lines** list (stored PVs, stepped
with ←/→) and a **Free play** mode for laying out your own reading — clearly
labeled as unverified, since nothing is computed client-side.

## 9. Troubleshooting

- **"I can't find my problems"** → all paths are anchored to the project
  directory regardless of where you run the scripts from, and the
  generator prints the absolute candidates/accepted paths at startup
  (`[paths] ...`). Candidates wait in `candidates/`, accepted ones move to
  `accepted/` (and into `web/problems.json`), rejected ones to
  `rejected/`. If a directory looks empty, check the startup lines of the
  process that wrote the files.

- **KataGo dies at startup** → run the smoke test from step 3; the generator
  also prints KataGo's last stderr lines on failure. Usually a wrong
  `-model`/`-config` path or missing GPU runtime.
- **First query takes minutes** → normal: OpenCL tuner / TensorRT engine
  build. Cached under `~/.katago/` afterwards.
- **Ownership looks inverted / everything is detected as dead** → shouldn't
  happen: the shipped `analysis.cfg` pins `reportAnalysisWinratesAs = BLACK`,
  and the client additionally runs two calibration probes at startup and
  prints the detected perspective (`black` / `white` / `sidetomove`). If you
  ever see nonsense, check that `[calibrate]` line first.
- **Out of VRAM** → lower `nnMaxBatchSize` in `analysis.cfg`.
- **Too few / low-quality candidates** → see the knob list in step 4; also
  simply run more games. Quality control is what the review step is for.
- **Review page says "Waiting for candidates"** → it only reads
  `candidates/*.json`; confirm the generator's `--out` points there.

## Design notes & honest limitations

- "Unconditionally alive/dead" here means *KataGo at high confidence with
  best play*, upgraded where possible: alive variants carry a formal
  Benson pass-alive certificate, and everything carries ko flags (§6).
  Ko-free dead verdicts remain statistical — there is no cheap formal
  dual of Benson for death.
- Killing/living move sets are exhaustive within the region (every empty
  point is deep-checked), so an unlisted vital point can only hide outside
  the `--margin` window — e.g. a distant ladder breaker. You can hand-edit
  a problem JSON to add alternates.
- A pass is inserted when the wrong side is to move at the hinge; for
  whole-board evaluation this is sound (the pass is by the side whose turn we
  are taking away), but it does mean komi/winrate metadata reflects the
  padded sequence, not the original game.
- Everything is keyed to full-board positions. If you want classic cropped
  corner diagrams, each problem carries a `region` bounding box around the
  target group — cropping in `goban.js` is a straightforward extension.
