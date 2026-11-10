---
title: Remix Triage
tags: [harness-0, research, remix]
updated: 2026-11-10
---

# Remix Triage

Every outside project harness-0 learned from gets a verdict: **copy** (fork it),
**scuttle** (take the parts that fit, re-implement them here, credit the source) or
**trash for learning** (keep the lesson, drop the thing, say why). Scuttle is the default.
No outside code was pasted into harness-0; every part listed as taken was re-implemented.

## Pi

**Scuttle.** Taken: the small core prompt, the short tool list, sessions as JSONL. Left:
TypeScript extensions (h0 extends through fittings, which are data), and the tree-shaped
session file, which is the obvious next step for `h0 --resume`. Licence MIT.

## pstack

Lauren Tan's plugin for Cursor and Claude Code, `cursor/plugins/pstack`, MIT, cloned at
`~/Work/pstack-upstream`. **Scuttle.** Taken: the trigger table, rules bound to the moment
they apply ("before commit → deslop"), which became h0's triggers. Noted for later: the
playbooks (investigation, bug fix, perf, hillclimb) are a natural layer for a fitting to
specialise, and the *guard the context window* principle is already the local profile.
Left: the multi-model delegation.

## build-omarchy-themes

Tom Ballard, MIT, cloned at `~/Work/build-omarchy-themes`. **Scuttle.** Taken: the
evaluation host contract (`host_metadata` first, typed events, `host_summary` last, every
injected file counted), `HOME` pointed at the fixture when the fixture is a home, and the
idea of pinned source contracts, which becomes a fitting's `sources.json` with hashes for
drift checks. Their Claude evaluation is frozen and unrun for want of a connected host;
`h0 host` is built to be one.

## Omarchy's official agent skill

**Copy, as data.** It is the source the Omarchy fitting is built from, read from the
installed package so it updates with Omarchy. One finding goes back the other way: its
`plugins.md` tells agents to clone a built-in widget to customise it, which led three
models into cloning the whole clock plugin for a one-field format change
([omarchy](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/results/omarchy.md)). A sentence saying that widget options in `shell.json` need no clone
would fix it upstream.

## revfactory/harness

**Trash for learning.** It generates Claude Code agent teams and skills per project, which
is configuration of someone else's harness and multi-agent by design. The lesson kept: its
author ran a controlled A/B, and the format of reporting per-dimension effects is worth
copying when quality, not pass/fail, is measured.

## directiveforge

**Scuttle.** Taken: pre-registration with the spec committed before any run, checked
against git history. That is PREREG.

## Meta-Harness and Harness²

**Scuttle (ideas only).** Taken: the proposer reads raw traces, not compressed summaries
(Meta-Harness), and proposals are tested against recorded trajectories before they are
kept (Harness²). h0's reflector reads per-trial tool calls and the paired vanilla run's
steps, and its gate is an eval run. Left: search over harness code; a fitting is data, so
h0 searches over data.

## LangChain Deep Agents

**Scuttle.** Taken: verify before finishing, loop detection (a warning after three
identical calls), the harness assembling context instead of making the model discover it.
Left: the "reasoning sandwich" (high effort to plan and verify, lower to implement), which
needs per-turn effort; the Anthropic API now supports changing effort mid-conversation, so
this is a near-term experiment.

## Anthropic sandbox-runtime and Codex's sandbox

**Scuttle (design only).** h0 uses bubblewrap directly. Left for later: the filtering
egress proxy that lets a granted command reach only an allowlist.

## Research Delight

A convergence harness for research: done is declared per field up front, gaps
are targeted, every value cites or flags. **Copy, as a sibling.** Its pluggable gatherer is
where h0 can run research on the API next.

## T3 Code's engineering standards

`~/Work/t3code/AGENTS.md` (Theo and Julius; 500,000 users). **Scuttle.** Three parts fit:

- **"The three ways to hurt yourself"** is the right shape for a fitting's hazards: a short
  list of project-specific actions that do damage, each with the reason. Its first entry,
  *killing by pattern* (`pkill -f` matches your own agent, whose argv holds the worktree
  path), happened during this build: a `pkill -f "h0 eval mine"` killed the shell that ran
  it. In h0 the sandbox makes it structural: every command gets a new PID namespace, so a
  pattern can only match the sandbox's own processes. A fitting's hazards belong in
  triggers (`before:bash`, `block`) rather than prose, for the same reason.
- **"Hit every surface"** (entry points, clients, providers, agents, contracts, reverse
  states, connection modes, docs) is a checklist a fitting's verify step could carry for
  projects with many surfaces; Terminal Delight is one.
- **"Reverse states: a way in needs a way out"** is applied in this build's records: the
  ollama tag, the keyring entry and the private repository each name their undo.

Theo's note (*ambitious ideas, simple systems … do not introduce machinery because it looks
architecturally impressive … measure twice, cut once, and yagni*) is the same stance as
this repository's no-dependency rule.

## Bespoke harness research

**Scuttle, ideas only.** The [Bespoke Harnesses intake](2026-11-10-bespoke-harness-leaders.md)
adds gskill's split discipline and skill transfer as references for `h0/learn.py` and
`h0/evals.py`; AutoSaddler's intervention classes and candidate provenance for
`h0/learn.py` and `h0/trace.py`; and Stripe's deterministic workflow steps for
`h0/loop.py` and fitting verification. These are proposed destinations, not implemented
changes. No source or prompt was copied, no dependency was added, and no licence
clearance for a later code import is implied. The intake links the primary sources,
their limits and the two follow-up issues.
