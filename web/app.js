/* App state machine.
 *
 * Modes:
 *   player  — loads the accepted pool, shuffled; no accept/reject.
 *   review  — ?review; polls review.cgi, shows metadata and
 *             accept/reject buttons (keys: a / r).
 *
 * Phases per problem:
 *   judge   — pick Alive / Dead / Undecided
 *   kill    — (undecided only) play the killing move
 *   live    — (undecided only) play the living move
 *   explore — step through precomputed lines / free play
 */
"use strict";

const QS = new URLSearchParams(location.search);
// Reviewing needs no credentials: append ?review to the URL.  The review UI
// is simply not linked from the page; that keeps it out of the way of
// ordinary visitors, it does not keep out anyone who is actually looking.
const REVIEW = QS.has("review");
const API = action => `review.cgi?action=${action}`;

/* Repository / deployment layout — this page lives in web/, the problem
 * data sits in sibling directories one level up:
 *
 *     dead-or-alive/
 *         web/          index.html, app.js, review.cgi, manifest*.json
 *         candidates/   awaiting review
 *         accepted/     live for players
 *         rejected/
 *
 * manifest.json indexes accepted/, manifest-candidates.json indexes
 * candidates/ — the server cannot list a directory, so the manifests are
 * the index.  review.cgi keeps them in step with the directories. */
const POOL = {
  accepted:   { manifest: "manifest.json",            dir: "../accepted" },
  candidates: { manifest: "manifest-candidates.json", dir: "../candidates" },
  rejected:   { manifest: "manifest-rejected.json",   dir: "../rejected" },
};

const $ = id => document.getElementById(id);
const boardEl = $("board");

const state = {
  pool: [],          // filenames (manifest mode) or indices (inline mode)
  dir: POOL.accepted.dir,  // directory the pool's filenames live in
  inline: null,      // the inline problems.json array, when used
  cache: {},         // filename -> fetched problem
  idx: 0,
  problem: null,
  candFile: null,
  phase: "judge",
  attempts: 0,
  line: null,        // legacy holder (kill/live solution playback)
  path: [],          // moves ACTUALLY on the board while exploring
  follow: null,      // optional seq the ▶ button walks along
  freePlay: false,
  freeToMove: "B",
  fullBoard: false,
  solvedKill: false,
  solvedLive: false,
};

const goban = new Goban(boardEl, 19, onBoardClick);

/* Crop the display to the problem region (classic tsumego framing) when
 * the fight covers a small part of a big board.  Snaps to nearby board
 * edges so corner problems show the actual corner. */
function cropView(p) {
  if (!p.region || p.boardSize < 13) return null;
  const N = p.boardSize, r = p.region, SNAP = 3, PADV = 1;
  let x0 = Math.max(0, r.x0 - PADV), y0 = Math.max(0, r.y0 - PADV);
  let x1 = Math.min(N - 1, r.x1 + PADV), y1 = Math.min(N - 1, r.y1 + PADV);
  if (x0 <= SNAP) x0 = 0;
  if (y0 <= SNAP) y0 = 0;
  if (x1 >= N - 1 - SNAP) x1 = N - 1;
  if (y1 >= N - 1 - SNAP) y1 = N - 1;
  // keep it usefully square-ish and at least 7x7
  const grow = (a, b, lim) => {
    while (b - a + 1 < 7) { if (a > 0) a--; else if (b < lim) b++; else break; }
    return [a, b];
  };
  [x0, x1] = grow(x0, x1, N - 1);
  [y0, y1] = grow(y0, y1, N - 1);
  const frac = ((x1 - x0 + 1) * (y1 - y0 + 1)) / (N * N);
  return frac < 0.55 ? { x0, y0, x1, y1 } : null;
}

function updateViewToggle() {
  const b = $("viewtoggle");
  if (!b) return;
  const p = state.problem;
  const croppable = p && cropView(p);
  b.style.display = croppable ? "" : "none";
  b.textContent = state.fullBoard ? "◱ crop" : "◰ full board";
}

function toggleView() {
  state.fullBoard = !state.fullBoard;
  goban.view = state.fullBoard ? null : cropView(state.problem);
  updateViewToggle();
  goban.draw();
}

/* ------------------------------------------------------------------ load */
async function init() {
  document.body.classList.toggle("review", REVIEW);
  if (REVIEW) {
    pollReview();
    setInterval(refreshStats, 4000);
  } else {
    const ok = await loadPool();
    if (!ok) {
      setMsg("No problems yet — web/manifest.json is missing or empty. " +
             "Accept some candidates first (open this page with ?review).", "warn");
      return;
    }
    loadProblem(0);
  }
}

/* Load the index of problem filenames, then fetch problems one at a time
   by name out of the matching directory.  ?pool=candidates reviews the
   unreviewed queue instead of the accepted pool.  Falls back to the inline
   problems.json (hand-made samples, sitting next to this file) when the
   manifest is missing or empty. */
async function loadPool() {
  const asked = QS.get("pool");
  const spec = POOL[asked || "accepted"];
  if (asked && !spec) return false;             // unknown pool name
  try {
    const r = await fetch(spec.manifest, { cache: "no-store" });
    if (r.ok) {
      const m = await r.json();
      const names = Array.isArray(m) ? m : (m.problems || []);
      if (names.length) {
        state.pool = names.slice();
        state.dir = spec.dir;
        state.inline = null;
        shuffle(state.pool);
        return true;
      }
    }
  } catch (e) { /* fall through to the inline pool */ }
  if (asked) return false;      // explicit pool asked for and not found
  try {
    const r = await fetch("problems.json", { cache: "no-store" });
    const arr = await r.json();
    if (!arr.length) return false;
    state.inline = arr;
    state.pool = arr.map((_, i) => i);
    shuffle(state.pool);
    return true;
  } catch (e) {
    return false;
  }
}

async function fetchProblem(key) {
  if (state.inline) return state.inline[key];
  if (state.cache[key]) return state.cache[key];
  const r = await fetch(`${state.dir}/${key}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`cannot load ${key}`);
  const p = await r.json();
  state.cache[key] = p;
  return p;
}

let pollTimer = null;
function retryPoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollReview, ms);
}

/* review.cgi reports configuration trouble in `warnings` rather than
   failing, so that a half-broken deployment is visible instead of just
   looking like an empty queue.  `awaiting_apply` counts decisions that have
   been recorded but not yet carried out: the CGI cannot move files on the
   live server, so rebuild_manifests.py does it afterwards. */
function setQMeta(d) {
  const n = (d.warnings || []).length;
  $("qmeta").textContent =
    `queue: ${d.pending ?? "?"} · accepted: ${d.accepted ?? "?"}` +
    (d.rejected == null ? "" : ` · rejected: ${d.rejected}`) +
    (d.awaiting_apply ? ` · to apply: ${d.awaiting_apply}` : "") +
    (n ? ` · ⚠ ${n}` : "");
  if (n) console.warn("review.cgi:", ...d.warnings);
}

async function pollReview() {
  clearTimeout(pollTimer);
  let res, text;
  try {
    res = await fetch(API("next"), { cache: "no-store" });
    text = await res.text();
  } catch (e) {
    setMsg(`Cannot reach review.cgi — ${e.message}.`, "warn");
    return retryPoll(5000);
  }
  let d;
  try {
    d = JSON.parse(text);
  } catch (e) {
    // Not JSON: the server handed back the script itself, or an error page.
    setMsg(text.startsWith("#!")
      ? "review.cgi was served as a plain file instead of being executed — " +
        "CGI is not enabled for this directory (Apache: Options +ExecCGI, " +
        "AddHandler cgi-script .cgi)."
      : `review.cgi returned HTTP ${res.status} and not JSON.`, "warn");
    return retryPoll(8000);
  }
  if (d.error) {
    setMsg(`review.cgi: ${d.error}${d.hint ? " — " + d.hint : ""}`, "warn");
    setQMeta(d);
    return retryPoll(8000);
  }
  if (d.empty) {
    state.problem = null;
    state.candFile = null;
    setQMeta(d);
    if ((d.warnings || []).length) setMsg(d.warnings.join("  ·  "), "warn");
    else if (d.awaiting_apply)
      setMsg(`Queue reviewed — ${d.awaiting_apply} decision(s) recorded and ` +
             `waiting for rebuild_manifests.py to be run on the server.`, "good");
    else setMsg(`No candidates in ${d.dir || "candidates/"} — the generator ` +
                `will drop them in as it finds them.`, "info");
    return retryPoll(2500);
  }
  if (!d.problem) {
    setMsg("review.cgi replied without a problem in it.", "warn");
    return retryPoll(8000);
  }
  if (d.file !== state.candFile) {
    state.candFile = d.file;
    setProblem(d.problem);
    setQMeta(d);
  }
}

async function refreshStats() {
  if (!REVIEW) return;
  try {
    const r = await fetch(API("stats"), { cache: "no-store" });
    const s = await r.json();
    if (!s.error) setQMeta(s);
  } catch (e) { /* pollReview is the one that reports failures */ }
}

async function decide(accept) {
  if (!state.candFile) return;
  try {
    const r = await fetch(API("decision"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: state.candFile, accept }),
    });
    const d = await r.json();
    if (d.error) {
      setMsg(`Decision not recorded: ${d.error}`, "bad");
      return;                        // keep candFile so it can be retried
    }
  } catch (e) {
    setMsg(`Decision not recorded — ${e.message}.`, "bad");
    return;
  }
  state.candFile = null;
  pollReview();
}

/* ------------------------------------------------------------- problems */
async function loadProblem(i) {
  if (!state.pool.length) return;
  const n = state.pool.length;
  state.idx = ((i % n) + n) % n;
  const key = state.pool[state.idx];
  try {
    const p = await fetchProblem(key);
    setProblem(p);
  } catch (e) {
    setMsg(`Could not load ${state.dir}/${key}.`, "warn");
    return;
  }
  $("counter").textContent = `${state.idx + 1} / ${n}`;
}

function setProblem(p) {
  state.problem = p;
  state.phase = "judge";
  state.attempts = 0;
  state.line = null;
  state.freePlay = false;
  state.solvedKill = state.solvedLive = false;
  goban.size = p.boardSize;
  goban.view = state.fullBoard ? null : cropView(p);
  updateViewToggle();
  goban.setPosition(p.initialStones);
  goban.interactive = false;
  goban.dim = false;
  goban.hints = [];
  goban.marks = targetMarks(p);
  if (p.lastMove) {
    const q = gtpToXY(p.lastMove[1], p.boardSize);
    goban.lastMove = q;
  }
  goban.draw();
  hideStamp();
  $("plate").textContent = plateLabel(p);
  setMsg(`Judge the marked ${p.targetColor === "B" ? "Black" : "White"} group — ` +
         `the verdict must hold no matter who moves first.`, "ask");
  showJudge(true);
  $("phasebar").innerHTML = "";
  $("stepbar").innerHTML = "";
  $("lines").innerHTML = "";
  renderMeta(p);
}

function plateLabel(p) {
  const m = p.meta || {};
  const g = m.gameIdx != null ? `G${String(m.gameIdx).padStart(2, "0")}` : "—";
  const mv = m.flipMove != null ? `M${m.flipMove}` : "";
  let s = `${g} · ${mv} · ${p.targetColor}${p.targetStones.length}`;
  if (m.quality != null) s += ` · Q${m.quality}`;
  if (m.bensonAlive) s += " · pass-alive ✓";
  if (m.koSuspect) s += " · ⚠ ko";
  return s;
}

function targetMarks(p) {
  return p.targetStones.map(mv => {
    const q = gtpToXY(mv, p.boardSize);
    return { x: q.x, y: q.y, type: "target" };
  });
}

/* --------------------------------------------------------------- judging */
function judge(answer) {
  const p = state.problem;
  if (!p || state.phase !== "judge") return;
  const right = answer === p.status;
  stamp(p.status, right);
  showJudge(false);
  if (!right) {
    setMsg(`No — this group is ${statusWord(p.status)}.`, "bad");
  } else {
    setMsg(`Correct — ${statusWord(p.status)}.`, "good");
  }
  if (p.status === "undecided") {
    if (right) {
      startKill();
      phaseButtons([
        ["Skip to living move", startLive],
        ["Explore lines", () => explore()],
      ]);
    } else {
      phaseButtons([
        ["Find the killing move", startKill],
        ["Find the living move", startLive],
        ["Explore lines", () => explore()],
      ]);
    }
  } else {
    // settled problem: go straight into interactive proof mode — the
    // board is clickable, a ghost stone of the challenging side follows
    // the mouse, and every precomputed attempt plays out its refutation
    explore();
    const chal = opp(p.targetColor);
    const who = p.status === "alive" ? colorWord(chal)
                                     : colorWord(p.targetColor);
    const task = p.status === "alive" ? "try to kill the marked group"
                                      : "try to save the marked group";
    setMsg(`${statusWord(p.status)}. ${who} to try: ${task} — click on ` +
           `the board; every precomputed attempt is refuted.`,
           right ? "good" : "bad");
    goban.setHoverColor(p.status === "alive" ? chal : p.targetColor);
    phaseButtons([
      ["Free play", toggleFree],
    ].concat(!REVIEW ? [["Next problem →", () => loadProblem(state.idx + 1)]] : []));
  }
}

function statusWord(s) {
  return s === "alive" ? "unconditionally alive — no matter who moves first"
       : s === "dead" ? "unconditionally dead — no matter who moves first"
       : "undecided — whoever moves first settles it";
}

/* -------------------------------------------------------- kill / live */
function startKill() {
  goban.hints = [];
  resetToBase();
  state.phase = "kill";
  goban.interactive = true;
  goban.setHoverColor(state.problem.killing.toMove);
  setMsg(`${colorWord(state.problem.killing.toMove)} to play — kill the marked group.`, "ask");
}

function startLive() {
  goban.hints = [];
  resetToBase();
  state.phase = "live";
  goban.interactive = true;
  goban.setHoverColor(state.problem.living.toMove);
  setMsg(`${colorWord(state.problem.living.toMove)} to play — save the marked group.`, "ask");
}

function resetToBase() {
  state.path = [];
  state.follow = null;
  const p = state.problem;
  goban.setPosition(p.initialStones);
  goban.marks = targetMarks(p);
  goban.dim = false;
  state.line = null;
  state.freePlay = false;
  $("stepbar").innerHTML = "";
  goban.draw();
}

function colorWord(c) { return c === "B" ? "Black" : "White"; }

function onBoardClick(x, y) {
  const p = state.problem;
  if (!p) return;
  if (state.freePlay) return freeMove(x, y);
  if (state.phase === "explore") return exploreClick(x, y);
  if (state.phase !== "kill" && state.phase !== "live") return;

  const spec = state.phase === "kill" ? p.killing : p.living;
  const mv = xyToGtp(x, y, p.boardSize);
  if (spec.moves.includes(mv)) {
    if (state.phase === "kill") state.solvedKill = true; else state.solvedLive = true;
    const line = spec.lines.find(l => l.first === mv) || spec.lines[0];
    playLine(line.seq, `Correct. Main line after ${mv}` +
      (spec.lines.length > 1 ? ` — also correct: ${spec.moves.filter(m => m !== mv).join(", ")}` : "") + ".");
    afterPhase();
  } else {
    state.attempts++;
    const phase = state.phase;
    const again = phase === "kill" ? startKill : startLive;
    const hintable = state.attempts >= 2;
    // a failing try that we precomputed: play it AND its refutation
    const ref = bookLines(p).find(l =>
      l.seq[0] && l.seq[0][0] === spec.toMove && l.seq[0][1] === mv);
    if (ref) {
      playLine(ref.seq, `${mv} fails — here is the refutation; the group ` +
        `ends up ${ref.result} (own ${ref.groupScore}).`);
      setMsg(`${mv} fails — step through the refutation (◀ ▶); the group ` +
             `ends up ${ref.result}.`, "bad");
      goban.interactive = false;
      state.phase = phase;                    // still solving this phase
      phaseButtons([["Try again", again]].concat(hintable ? [
        ["Hint", () => { again(); hint(spec); }],
        ["Show solution", () => showSolution(spec)],
      ] : []));
      return;
    }
    goban.marks = targetMarks(p).concat([{ x, y, type: "wrong" }]);
    goban.draw();
    setMsg(`${mv} is not in the solution set for this problem` +
      (hintable ? " — press Hint or Show solution." : ". Try again."), "bad");
    phaseButtons(hintable ? [
      ["Hint", () => hint(spec)],
      ["Show solution", () => showSolution(spec)],
    ] : []);
  }
}

function hint(spec) {
  const p = state.problem;
  const q = gtpToXY(spec.moves[0], p.boardSize);
  goban.marks = targetMarks(p).concat([{ x: q.x, y: q.y, type: "hint" }]);
  goban.draw();
}

function showSolution(spec) {
  const line = spec.lines[0];
  playLine(line.seq, `Solution: ${spec.moves.join(" / ")}.`);
  afterPhase();
}

function afterPhase() {
  const btns = [];
  if (state.phase === "kill" && !state.solvedLive)
    btns.push(["Now find the living move", startLive]);
  if (state.phase === "live" && !state.solvedKill)
    btns.push(["Now find the killing move", startKill]);
  btns.push(["Explore lines", () => explore()], ["Free play", toggleFree]);
  if (!REVIEW) btns.push(["Next problem →", () => loadProblem(state.idx + 1)]);
  phaseButtons(btns);
  goban.interactive = false;
}

/* ------------------------------------------------------------ line play */
function playLine(seq, label) {
  state.follow = seq;
  state.path = seq.slice(0, Math.min(2, seq.length));
  renderStepper();
  renderPath();
  setMsg(label + " Step with ◀ ▶ (or ←/→).", "good");
}

function renderStepper() {
  $("stepbar").innerHTML = "";
  const bar = document.createElement("div");
  bar.className = "stepper";
  const back = mkbtn("◀", stepBack);
  const fwd = mkbtn("▶", stepFwd);
  const pos = document.createElement("span");
  pos.id = "steppos"; pos.className = "steppos";
  bar.append(back, pos, fwd);
  $("stepbar").appendChild(bar);
  updateStepPos();
}

/* Replay state.path from the base position.  The board is ALWAYS exactly
   the moves in state.path — nothing else — so a click can never teleport
   the position into an unrelated variation. */
function renderPath() {
  const p = state.problem;
  if (!p) return;
  goban.setPosition(p.initialStones);
  goban.marks = targetMarks(p);
  state.path.forEach(([c, mv], i) => {
    const r = goban.board.playGtp(c, mv);
    if (r.pass || !r.ok) return;          // passes and (rare) illegal PV moves
    const q = gtpToXY(mv, p.boardSize);
    goban.marks = goban.marks.filter(m =>
      // drop numbers whose stone has been captured ...
      (m.type !== "num" || goban.board.get(m.x, m.y) !== ".") &&
      // ... and any mark on the point being played now, so a ko recapture
      // cannot stack two numbers on one stone
      !(m.x === q.x && m.y === q.y));
    goban.marks.push({ x: q.x, y: q.y, type: "num", label: i + 1 });
  });
  // a numbered stone already shows which move was last; the extra last-move
  // dot on top of it read as a second marking
  goban.lastMove = null;
  // the group under investigation is only marked where it still stands
  goban.marks = goban.marks.filter(m =>
    m.type !== "target" || goban.board.get(m.x, m.y) === p.targetColor);
  updateHints();
  goban.draw();
  updateStepPos();
}

function stepBack() {
  if (!state.path.length) return;
  state.path.pop();
  renderPath();
}

function stepFwd() {
  const p = state.problem, f = state.follow;
  if (f && f.length > state.path.length && prefixEq(f, state.path)) {
    state.path.push(f[state.path.length]);       // continue the shown line
  } else {
    const cont = continuations(p, state.path);   // or the best book move
    if (!cont.size) return;
    const [mv, e] = [...cont.entries()][0];
    state.path.push([e.color, mv]);
    state.follow = e.lines[0].seq;
  }
  renderPath();
}

function updateStepPos() {
  const el = $("steppos");
  if (!el) return;
  const f = state.follow;
  const total = (f && prefixEq(f, state.path)) ? `/${f.length}` : "";
  el.textContent = `${state.path.length}${total}`;
}

/* --------------------------------------------------------------- explore */
function bookLines(p) {
  const out = [];
  if (p.killing) for (const l of p.killing.lines)
    out.push({ seq: l.seq, result: "dead", groupScore: l.groupScore });
  if (p.living) for (const l of p.living.lines)
    out.push({ seq: l.seq, result: "alive", groupScore: l.groupScore });
  for (const l of (p.explanationLines || []))
    if (l.seq && l.seq.length)
      out.push({ seq: l.seq, result: l.result, groupScore: l.groupScore });
  // weak solution: every plausible challenger try with its single refuting
  // reply, so clicking any half-sensible wrong move shows why it fails
  const ws = p.weakSolution || {};
  for (const key of Object.keys(ws))
    for (const t of ws[key])
      if (t.seq && t.seq.length)
        out.push({ seq: t.seq, result: t.result, groupScore: t.groupScore });
  // dedup identical sequences (a solution move may also appear as a try)
  const seen = new Set(), uniq = [];
  for (const l of out) {
    const k = JSON.stringify(l.seq);
    if (!seen.has(k)) { seen.add(k); uniq.push(l); }
  }
  return uniq;
}

function prefixEq(seq, prefix) {
  for (let i = 0; i < prefix.length; i++)
    if (!seq[i] || seq[i][0] !== prefix[i][0] || seq[i][1] !== prefix[i][1])
      return false;
  return true;
}

/* Every book move available from `path`, grouped by point.  A move is only
   offered if some stored line actually continues from EXACTLY this
   position, so exploring can never jump into an unrelated variation. */
function sideToMove(p, path) {
  // after any move the colour alternates; at the root it is the side that
  // is trying to overturn the verdict, so exploring starts with the moves
  // that matter (and the same point is never offered for both colours)
  return path.length ? opp(path[path.length - 1][0]) : challengerColor(p);
}

function continuations(p, path) {
  const side = sideToMove(p, path);
  const out = new Map();
  for (const l of bookLines(p)) {
    if (l.seq.length <= path.length || !prefixEq(l.seq, path)) continue;
    const [c, mv] = l.seq[path.length];
    if (c !== side) continue;              // never mix colours on one point
    let e = out.get(mv);
    if (!e) { e = { color: c, lines: [] }; out.set(mv, e); }
    e.lines.push(l);
  }
  for (const e of out.values())
    e.lines.sort((a, b) => Math.abs(b.groupScore ?? 0) - Math.abs(a.groupScore ?? 0));
  return out;
}

/* Whose move is the "interesting" one for this problem: the side trying to
   overturn the verdict. */
function challengerColor(p) {
  if (p.status === "alive") return opp(p.targetColor);   // attacker tries to kill
  if (p.status === "dead") return p.targetColor;         // defender tries to live
  return opp(p.targetColor);                             // undecided: attacker first
}

function updateHints() {
  if (state.phase !== "explore" || !state.problem) { goban.hints = []; return; }
  const p = state.problem;
  const hints = [];
  for (const [mv, e] of continuations(p, state.path)) {
    const q = gtpToXY(mv, p.boardSize);
    if (q) hints.push({ x: q.x, y: q.y, color: e.color });
  }
  goban.hints = hints;
  goban.setHoverColor(sideToMove(p, state.path));
}

function lineVerdict(l) {
  const g = l.groupScore == null ? "" : ` (own ${l.groupScore})`;
  return l.result === "alive" ? `the group lives${g}`
       : l.result === "dead" ? `the group dies${g}`
       : `unclear${g}`;
}

function exploreClick(x, y) {
  const p = state.problem;
  const mv = xyToGtp(x, y, p.boardSize);
  const e = continuations(p, state.path).get(mv);
  if (!e) {
    // NOT in the book from here: mark it and leave the position alone.
    const legal = goban.board.get(x, y) === ".";
    goban.marks = goban.marks.filter(m => m.type !== "wrong");
    if (legal) goban.marks.push({ x, y, type: "wrong" });
    goban.draw();
    setMsg(legal
      ? `${mv} wasn't analysed from this position — the faint dots mark what is stored.`
      : `${mv} is occupied.`, "warn");
    return;
  }
  state.path.push([e.color, mv]);
  state.follow = e.lines[0].seq;
  // if every stored line answers this the same way, play the reply too
  const nxt = continuations(p, state.path);
  if (nxt.size === 1) {
    const [rmv, re] = [...nxt.entries()][0];
    state.path.push([re.color, rmv]);
    state.follow = re.lines[0].seq;
  }
  renderPath();
  const l = e.lines[0];
  const more = continuations(p, state.path).size > 0;
  setMsg(`${mv}: ${lineVerdict(l)}` +
         (more ? " — keep going, or ◀ to take it back." : " · end of this line."),
         l.result === "unclear" ? "warn"
           : l.result === "alive" ? "good" : "bad");
}

function explore() {
  const p = state.problem;
  state.phase = "explore";
  goban.interactive = true;
  state.path = [];
  state.follow = null;
  const box = $("lines");
  box.innerHTML = "";
  // the precomputed list can run to hundreds of lines: keep it folded away
  const det = document.createElement("details");
  det.className = "linefold";
  const sum = document.createElement("summary");
  det.appendChild(sum);
  box.appendChild(det);
  const host = document.createElement("div");
  det.appendChild(host);
  let count = 0;
  const add = (title, seq, tag) => {
    const b = document.createElement("button");
    b.className = "line";
    b.innerHTML = `<span class="tag ${tag}">${tag}</span> ${title}`;
    b.onclick = () => playLine(seq, title + ".");
    host.appendChild(b);
    count++;
  };
  if (p.status === "undecided") {
    for (const l of p.killing.lines)
      add(`Kill with ${l.first} (own ${l.groupScore})`, l.seq, "dead");
    for (const l of p.living.lines)
      add(`Live with ${l.first} (own ${l.groupScore})`, l.seq, "alive");
    (p.explanationLines || []).forEach(l => {
      if (l.seq && l.seq.length)
        add(`Failed try ${l.seq[0][1]} → ${l.result}`, l.seq,
            l.result === "alive" ? "alive" : l.result === "dead" ? "dead" : "un");
    });
  } else {
    (p.explanationLines || []).forEach((l, i) => {
      const tag = l.result === "alive" ? "alive" : l.result === "dead" ? "dead" : "un";
      if (!l.seq || !l.seq.length) {          // note-only line
        const d = document.createElement("div");
        d.className = "line";
        d.innerHTML = `<span class="tag ${tag}">${tag}</span> ${l.note || "(no line)"}`;
        host.appendChild(d);
        count++;
        return;
      }
      add(l.note || `Try ${i + 1}: ${l.seq[0][1]} → group ${l.result}`, l.seq, tag);
    });
  }
  sum.textContent = `${count} precomputed line${count === 1 ? "" : "s"}`;
  if (!count) {
    box.innerHTML = "";
    setMsg("No stored lines for this one.", "warn");
    goban.interactive = false;
    state.phase = "done";
    return;
  }
  setMsg("Click a line below — or play on the board: the faint dots mark " +
         "every move that was analysed from the current position.", "info");
  state.path = [];
  state.follow = null;
  renderStepper();
  renderPath();
}

/* ------------------------------------------------------------- free play */
function toggleFree() {
  state.freePlay = !state.freePlay;
  if (state.freePlay) {
    resetToBase();
    state.freePlay = true;
    state.freeToMove = state.problem.toMove || "B";
    goban.interactive = true;
    goban.setHoverColor(state.freeToMove);
    setMsg("Free play (no engine — precomputed lines only are verified). " +
           "Click to alternate moves; Reset to return.", "info");
    phaseButtons([["Reset", resetFree], ["Stop free play", toggleFree]]);
  } else {
    goban.interactive = false;
    setProblem(state.problem);
  }
}

function resetFree() {
  resetToBase();
  state.freePlay = true;
  state.freeToMove = state.problem.toMove || "B";
  goban.interactive = true;
  goban.setHoverColor(state.freeToMove);
}

function freeMove(x, y) {
  const r = goban.board.play(state.freeToMove, x, y);
  if (!r.ok) { setMsg(`Illegal move (${r.reason}).`, "bad"); return; }
  goban.lastMove = { x, y };
  state.freeToMove = opp(state.freeToMove);
  goban.setHoverColor(state.freeToMove);
  goban.draw();
}

/* ---------------------------------------------------------------- chrome */
function showJudge(v) { $("judge").style.display = v ? "" : "none"; }

function phaseButtons(pairs) {
  const bar = $("phasebar");
  bar.innerHTML = "";
  for (const [label, fn] of pairs) bar.appendChild(mkbtn(label, fn));
}
function mkbtn(label, fn) {
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = fn;
  return b;
}

function setMsg(text, kind) {
  const el = $("msg");
  el.textContent = text;
  el.className = "msg " + (kind || "");
}

function stamp(status, right) {
  const el = $("stamp");
  el.textContent = status === "alive" ? "生" : status === "dead" ? "死" : "未";
  el.className = "stamp show " + status + (right ? " right" : " wrongish");
}
function hideStamp() { $("stamp").className = "stamp"; }

function renderMeta(p) {
  const el = $("meta");
  if (!REVIEW) { el.innerHTML = ""; return; }
  const m = p.meta || {};
  const rows = [
    ["id", p.id], ["status", p.status], ["to move", p.toMove],
    ["group", `${p.targetColor} × ${p.targetStones.length}`],
    ["flip", `${m.scoreBefore} → ${m.scoreAfter} @ move ${m.flipMove}`],
    ["kill moves", p.killing ? p.killing.moves.join(", ") : "—"],
    ["live moves", p.living ? p.living.moves.join(", ") : "—"],
    ["settled by", m.settledBy ? m.settledBy.join(" ") : "—"],
    ["quality", m.quality != null ? String(m.quality) +
        (m.qualityComponents ? "  " + Object.entries(m.qualityComponents)
          .filter(([, val]) => val)
          .map(([k, val]) => `${k} ${val > 0 ? "+" : ""}${val}`).join(", ") : "")
        : "—"],
    ["pass-alive", m.bensonAlive == null ? "—" : (m.bensonAlive ? "✓ (Benson)" : "no")],
    ["ko suspect", m.koSuspect ? "⚠ yes" : "no"],
    ["revalidated", m.revalidation ? (m.revalidation.ok ? "ok" :
        "FAILED: " + (m.revalidation.reasons || []).join("; ")) : "—"],
  ];
  el.innerHTML = rows.map(([k, v]) =>
    `<div class="mrow"><span>${k}</span><b>${v ?? "—"}</b></div>`).join("");
}

/* ------------------------------------------------------------------ keys */
document.addEventListener("keydown", e => {
  if (e.key === "ArrowLeft") stepBack();
  if (e.key === "ArrowRight") stepFwd();
  if (REVIEW && e.key === "a") decide(true);
  if (REVIEW && e.key === "r") decide(false);
  if (!REVIEW && e.key === "n") loadProblem(state.idx + 1);
});

$("btnAlive").onclick = () => judge("alive");
$("btnDead").onclick = () => judge("dead");
$("btnUndec").onclick = () => judge("undecided");
if ($("btnAccept")) $("btnAccept").onclick = () => decide(true);
if ($("btnReject")) $("btnReject").onclick = () => decide(false);
if ($("viewtoggle")) $("viewtoggle").onclick = toggleView;
if ($("btnPrev")) $("btnPrev").onclick = () => loadProblem(state.idx - 1);
if ($("btnNext")) $("btnNext").onclick = () => loadProblem(state.idx + 1);

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
}

init();
