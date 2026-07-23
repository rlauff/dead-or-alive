/* Goban: canvas rendering + enough Go rules to validate user moves.
 *
 * Coordinates: (x, y) with y = 0 at the TOP row (matches the generator).
 * GTP strings like "Q16" have row 1 at the bottom; letter I is skipped.
 */
"use strict";

const GTP_COLS = "ABCDEFGHJKLMNOPQRST";

function gtpToXY(move, size) {
  const m = String(move).trim().toUpperCase();
  if (m === "PASS" || m === "") return null;
  return { x: GTP_COLS.indexOf(m[0]), y: size - parseInt(m.slice(1), 10) };
}
function xyToGtp(x, y, size) {
  return GTP_COLS[x] + (size - y);
}
function opp(c) { return c === "B" ? "W" : "B"; }

/* ------------------------------------------------------------ board rules */
class GoBoard {
  constructor(size) {
    this.size = size;
    this.grid = Array.from({ length: size }, () => Array(size).fill("."));
    this.koPoint = null; // {x,y} illegal for the side to move, or null
  }
  clone() {
    const b = new GoBoard(this.size);
    b.grid = this.grid.map(r => r.slice());
    b.koPoint = this.koPoint ? { ...this.koPoint } : null;
    return b;
  }
  get(x, y) { return this.grid[y][x]; }
  inBounds(x, y) { return x >= 0 && x < this.size && y >= 0 && y < this.size; }
  neighbors(x, y) {
    const out = [];
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, ny = y + dy;
      if (this.inBounds(nx, ny)) out.push([nx, ny]);
    }
    return out;
  }
  group(x, y) {
    const color = this.get(x, y);
    const stones = [], libs = new Set(), seen = new Set([y * this.size + x]);
    const stack = [[x, y]];
    while (stack.length) {
      const [px, py] = stack.pop();
      stones.push([px, py]);
      for (const [nx, ny] of this.neighbors(px, py)) {
        const c = this.get(nx, ny);
        const key = ny * this.size + nx;
        if (c === ".") libs.add(key);
        else if (c === color && !seen.has(key)) { seen.add(key); stack.push([nx, ny]); }
      }
    }
    return { stones, libs };
  }
  /* Try to play; returns {ok, captured:[{x,y,color}], reason} without
   * mutating on failure. */
  play(color, x, y) {
    if (this.get(x, y) !== ".") return { ok: false, reason: "occupied" };
    if (this.koPoint && this.koPoint.x === x && this.koPoint.y === y)
      return { ok: false, reason: "ko" };
    const other = opp(color);
    this.grid[y][x] = color;
    const captured = [];
    for (const [nx, ny] of this.neighbors(x, y)) {
      if (this.get(nx, ny) === other) {
        const g = this.group(nx, ny);
        if (g.libs.size === 0) {
          for (const [sx, sy] of g.stones) {
            this.grid[sy][sx] = ".";
            captured.push({ x: sx, y: sy, color: other });
          }
        }
      }
    }
    const own = this.group(x, y);
    if (own.libs.size === 0) {           // suicide: revert
      this.grid[y][x] = ".";
      for (const c of captured) this.grid[c.y][c.x] = c.color;
      return { ok: false, reason: "suicide" };
    }
    // simple ko: single-stone capture by a single stone in atari
    this.koPoint = null;
    if (captured.length === 1 && own.stones.length === 1 && own.libs.size === 1) {
      this.koPoint = { x: captured[0].x, y: captured[0].y };
    }
    return { ok: true, captured };
  }
  playGtp(color, move) {
    const p = gtpToXY(move, this.size);
    if (!p) { this.koPoint = null; return { ok: true, captured: [], pass: true }; }
    return this.play(color, p.x, p.y);
  }
}

/* --------------------------------------------------------------- renderer */
class Goban {
  constructor(canvas, size, onClick) {
    this.canvas = canvas;
    this.size = size;
    this.onClick = onClick;
    this.board = new GoBoard(size);
    this.lastMove = null;      // {x,y}
    this.marks = [];           // [{x,y,type:'target'|'wrong'|'hint', label?}]
    this.hover = null;
    this.hoverColor = "B";
    this.defaultHoverColor = "B";   // colour used away from hint points
    this.hints = [];        // [{x,y,color}] explorable continuations
    this.interactive = false;
    this.dim = false;
    this.view = null;          // {x0,y0,x1,y1} crop window or null = full

    canvas.addEventListener("pointermove", e => this._hover(e));
    canvas.addEventListener("pointerleave", () => { this.hover = null; this.draw(); });
    canvas.addEventListener("click", e => {
      const p = this._eventPoint(e);
      if (p && this.onClick) this.onClick(p.x, p.y);
    });
    new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
  }

  setPosition(initialStones) {
    this.board = new GoBoard(this.size);
    for (const [c, mv] of initialStones) {
      const p = gtpToXY(mv, this.size);
      if (p) this.board.grid[p.y][p.x] = c;
    }
    this.lastMove = null;
    this.draw();
  }

  _view() {
    return this.view || { x0: 0, y0: 0, x1: this.size - 1, y1: this.size - 1 };
  }
  _metrics() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const px = Math.min(rect.width, 720);
    const dpr = window.devicePixelRatio || 1;
    const v = this._view();
    const vw = v.x1 - v.x0 + 1, vh = v.y1 - v.y0 + 1;
    const pad = px / (Math.max(vw, vh) + 1) * 0.9;
    const cell = (px - 2 * pad) / (Math.max(vw, vh) - 1 || 1);
    const w = 2 * pad + (vw - 1) * cell;
    const hgt = 2 * pad + (vh - 1) * cell;
    return { px, dpr, pad, cell, v, w, hgt };
  }
  _eventPoint(e) {
    const { pad, cell, v } = this._metrics();
    const r = this.canvas.getBoundingClientRect();
    const x = v.x0 + Math.round((e.clientX - r.left - pad) / cell);
    const y = v.y0 + Math.round((e.clientY - r.top - pad) / cell);
    if (!this.board.inBounds(x, y)) return null;
    if (x < v.x0 || x > v.x1 || y < v.y0 || y > v.y1) return null;
    return { x, y };
  }
  setHoverColor(c) { this.hoverColor = c; this.defaultHoverColor = c; }

  _hover(e) {
    if (!this.interactive) { if (this.hover) { this.hover = null; this.draw(); } return; }
    const p = this._eventPoint(e);
    const changed = JSON.stringify(p) !== JSON.stringify(this.hover);
    this.hover = p;
    // the ghost stone shows the colour that would actually be played here
    if (p) {
      const h = this.hints.find(k => k.x === p.x && k.y === p.y);
      this.hoverColor = h ? h.color : this.defaultHoverColor;
    }
    if (changed) this.draw();
  }

  draw() {
    const { px, dpr, pad, cell, v, w, hgt } = this._metrics();
    const vx = x => pad + (x - v.x0) * cell;
    const vy = y => pad + (y - v.y0) * cell;
    const c = this.canvas, ctx = c.getContext("2d");
    c.width = w * dpr; c.height = hgt * dpr;
    c.style.width = w + "px"; c.style.height = hgt + "px";
    ctx.scale(dpr, dpr);

    // wood
    const g = ctx.createLinearGradient(0, 0, w, hgt);
    g.addColorStop(0, "#c9a35e"); g.addColorStop(1, "#b9924f");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, hgt);

    // grid — lines run half a cell past the window on cut (non-edge) sides
    const cutL = v.x0 > 0, cutR = v.x1 < this.size - 1;
    const cutT = v.y0 > 0, cutB = v.y1 < this.size - 1;
    const xa = cutL ? pad - cell * 0.55 : pad;
    const xb = cutR ? w - pad + cell * 0.55 : w - pad;
    const ya = cutT ? pad - cell * 0.55 : pad;
    const yb = cutB ? hgt - pad + cell * 0.55 : hgt - pad;
    ctx.strokeStyle = "#4a3617";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let y = v.y0; y <= v.y1; y++) {
      ctx.moveTo(xa, vy(y)); ctx.lineTo(xb, vy(y));
    }
    for (let x = v.x0; x <= v.x1; x++) {
      ctx.moveTo(vx(x), ya); ctx.lineTo(vx(x), yb);
    }
    ctx.stroke();
    // fade the cut sides so the crop reads as a window, not an edge
    for (const [cut, x0, y0, x1, y1, gx, gy] of [
      [cutL, 0, 0, pad, hgt, 1, 0], [cutR, w - pad, 0, w, hgt, -1, 0],
      [cutT, 0, 0, w, pad, 0, 1], [cutB, 0, hgt - pad, w, hgt, 0, -1]]) {
      if (!cut) continue;
      const fg = ctx.createLinearGradient(
        gx ? (gx > 0 ? 0 : w) : 0, gy ? (gy > 0 ? 0 : hgt) : 0,
        gx ? (gx > 0 ? pad : w - pad) : 0, gy ? (gy > 0 ? pad : hgt - pad) : 0);
      fg.addColorStop(0, "rgba(185,146,79,1)");
      fg.addColorStop(1, "rgba(185,146,79,0)");
      ctx.fillStyle = fg;
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    }

    // hoshi
    const h = this.size === 19 ? [3, 9, 15] : this.size === 13 ? [3, 6, 9] : [2, this.size - 3];
    ctx.fillStyle = "#4a3617";
    for (const hx of h) for (const hy of h) {
      if (hx < v.x0 || hx > v.x1 || hy < v.y0 || hy > v.y1) continue;
      ctx.beginPath();
      ctx.arc(vx(hx), vy(hy), cell * 0.11, 0, 7);
      ctx.fill();
    }

    // coordinates
    ctx.fillStyle = "rgba(58,44,20,.65)";
    ctx.font = `${Math.max(9, cell * 0.34)}px ui-monospace, monospace`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (let x = v.x0; x <= v.x1; x++)
      ctx.fillText(GTP_COLS[x], vx(x), pad * 0.35);
    for (let y = v.y0; y <= v.y1; y++)
      ctx.fillText(String(this.size - y), pad * 0.35, vy(y));

    // stones
    const r = cell * 0.47;
    for (let y = v.y0; y <= v.y1; y++) for (let x = v.x0; x <= v.x1; x++) {
      const s = this.board.get(x, y);
      if (s === ".") continue;
      this._stone(ctx, vx(x), vy(y), r, s, 1);
    }

    // explorable continuations: barely-visible dots, so the board hints at
    // what can be clicked without telling you which move is right
    for (const h of this.hints) {
      if (h.x < v.x0 || h.x > v.x1 || h.y < v.y0 || h.y > v.y1) continue;
      if (this.board.get(h.x, h.y) !== ".") continue;
      ctx.beginPath();
      ctx.arc(vx(h.x), vy(h.y), cell * 0.085, 0, Math.PI * 2);
      ctx.fillStyle = h.color === "B" ? "rgba(20,22,26,0.20)"
                                      : "rgba(232,226,212,0.26)";
      ctx.fill();
    }

    // hover ghost
    if (this.interactive && this.hover && this.board.get(this.hover.x, this.hover.y) === ".") {
      this._stone(ctx, vx(this.hover.x), vy(this.hover.y), r,
                  this.hoverColor, 0.45);
    }

    // marks
    for (const m of this.marks) {
      if (m.x < v.x0 || m.x > v.x1 || m.y < v.y0 || m.y > v.y1) continue;
      const cx = vx(m.x), cy = vy(m.y);
      if (m.type === "target") {
        const on = this.board.get(m.x, m.y);
        ctx.strokeStyle = on === "B" ? "#e8e2d4" : "#20242b";
        ctx.lineWidth = Math.max(1.4, cell * 0.07);
        ctx.beginPath(); // triangle
        for (let k = 0; k < 3; k++) {
          const a = -Math.PI / 2 + k * 2 * Math.PI / 3;
          const tx = cx + Math.cos(a) * r * 0.55, ty = cy + Math.sin(a) * r * 0.55;
          k ? ctx.lineTo(tx, ty) : ctx.moveTo(tx, ty);
        }
        ctx.closePath(); ctx.stroke();
      } else if (m.type === "wrong") {
        ctx.strokeStyle = "#c8553d";
        ctx.lineWidth = Math.max(2, cell * 0.1);
        const d = r * 0.5;
        ctx.beginPath();
        ctx.moveTo(cx - d, cy - d); ctx.lineTo(cx + d, cy + d);
        ctx.moveTo(cx + d, cy - d); ctx.lineTo(cx - d, cy + d);
        ctx.stroke();
      } else if (m.type === "hint") {
        ctx.strokeStyle = "#57b891";
        ctx.lineWidth = Math.max(2, cell * 0.09);
        ctx.beginPath(); ctx.arc(cx, cy, r * 0.7, 0, 7); ctx.stroke();
      } else if (m.type === "num") {
        const on = this.board.get(m.x, m.y);
        ctx.fillStyle = on === "B" ? "#e8e2d4" : "#20242b";
        ctx.font = `600 ${cell * 0.45}px ui-monospace, monospace`;
        ctx.fillText(String(m.label), cx, cy + cell * 0.02);
      }
    }

    // last move
    if (this.lastMove) {
      const { x, y } = this.lastMove;
      const on = this.board.get(x, y);
      if (on !== "." && x >= v.x0 && x <= v.x1 && y >= v.y0 && y <= v.y1) {
        ctx.fillStyle = on === "B" ? "#e8e2d4" : "#20242b";
        ctx.beginPath();
        ctx.arc(vx(x), vy(y), r * 0.28, 0, 7);
        ctx.fill();
      }
    }

    if (this.dim) {
      ctx.fillStyle = "rgba(20,22,26,.35)";
      ctx.fillRect(0, 0, w, hgt);
    }
  }

  _stone(ctx, cx, cy, r, color, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha;
    const g = ctx.createRadialGradient(cx - r * 0.35, cy - r * 0.4, r * 0.15,
                                       cx, cy, r * 1.05);
    if (color === "B") { g.addColorStop(0, "#3a3f46"); g.addColorStop(1, "#0d0f13"); }
    else { g.addColorStop(0, "#ffffff"); g.addColorStop(1, "#d8d2c2"); }
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
    if (color === "W") {
      ctx.strokeStyle = "rgba(90,80,60,.45)"; ctx.lineWidth = 0.8;
      ctx.stroke();
    }
    ctx.restore();
  }
}
