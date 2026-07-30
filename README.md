# 死活 · dead-or-alive

A life-and-death status trainer for Go. You are shown a position with one group
marked by triangles, and you answer a single question:

> Is this group **unconditionally alive**, **unconditionally dead**, or is it
> **undecided** — settled by whoever moves first?

Everything is precomputed. Variations come from KataGo analysis stored in the
problem files; the page itself does no engine calls and needs no server-side
computation — it is static files plus one small CGI script for the review queue.

The problems where automatically generated using KataGo self play, watching its ownership map for flips.

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


