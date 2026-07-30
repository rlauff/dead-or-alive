# 死活 · dead-or-alive

A life-and-death status trainer for Go. You are shown a position with one group
marked by triangles, and you answer a single question:

> Is this group **unconditionally alive**, **unconditionally dead**, or is it
> **undecided** — settled by whoever moves first?

That third option is the point of the exercise. Most tsumego sets ask "find the
move"; this one asks you to read the position well enough to say whether a move
is even needed, before it tells you whose turn it is.


Everything is precomputed. Variations come from KataGo analysis stored in the
problem files; the page itself does no engine calls and needs no server-side
computation — it is static files plus one small CGI script for the review queue.

## How a problem plays out

1. **Judge.** Pick alive / dead / undecided. The verdict must hold no matter who
   moves first, and you are told the answer either way.
2. **Prove it.** For an *undecided* group you then have to find both the killing
   move and the living move; wrong tries that were analysed get played out
   together with their refutation, so you see *why* they fail. After two failed
   tries, hints unlock.
3. **Explore.** For a settled group the board goes straight into interactive
   proof mode: faint dots mark every point that was analysed from the current
   position, and each attempt plays its refutation. The stored lines are also
   listed, and free play is one click away.

The board only accepts legal moves — `goban.js` implements captures, suicide and
simple ko, so a click can never leave the position in a state the analysis does
not cover.

