---
title: Harness Principles
tags: [harness-0, rationale]
updated: 2026-11-08
---

# Harness Principles

A harness is everything between a model and the work: the system prompt, the tools,
the sandbox, the permission rules, the loop that decides when to stop, and whatever
memory it keeps. The question on the night this was written was whether that
layer should be the same for every project, the way Claude Code and Codex are, or
fitted to the one project it works on. Each principle below says what the evidence
outside says, what our own record says, and what harness-0's runs said when we
tested it. Where our runs contradict the principle, the contradiction stays in.

Sources are linked inline; the full list with verified/unverified marks is in
[landscape 2026](../research/landscape-2026.md).

## 1. A small core, and only what pays on top of it

Pi's whole design is a tiny, stable core that the user extends; its system prompt is
a few hundred tokens and it ships four tools. A controlled study of `AGENTS.md`-style
context files ([arXiv 2602.11988](https://arxiv.org/html/2602.11988v3)) found agents do
follow them, that they cost over 20% more, and that they do not generally raise
success. A month of our own sessions showed the same shape: a growing rulebook, flat rework.

h0's core prompt is about 230 tokens. A fitting's map is capped at 160 lines and its
lessons at twelve.

**What our runs said.** On the Omarchy suite the generated fitting raised prompt tokens
by a median 1.24× (Opus) to 1.69× (Haiku) per task and raised success on none of three
models ([omarchy](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/results/omarchy.md)). The cost of a fitting is real and per-turn; its benefit
has to be shown, task by task.

## 2. A rule has to arrive at the moment it applies

In our own sessions, rules carried by a hook or a skill held, and a rule carried by
prose alone faded within three months. pstack ([remix triage](../research/remix-triage.md#pstack)) is built as a trigger table for
the same reason ("before commit, run deslop").

h0 compiles this into the loop. A fitting's triggers fire before or after a tool call
whose command or path matches, and either inject a note, run a named check first, or
block the call.

T3 Code's `AGENTS.md` shows the same idea from the project side: its "three ways to hurt
yourself" are hazards with reasons, written for the exact moment an agent is about to do
them. The first (never kill a process by pattern, because your own agent's argv matches)
happened during this build. h0 removes it by construction: each sandboxed command gets a new
PID namespace, so a pattern can only match the sandbox's own processes.

**What our runs said.** Timing alone is not enough. Omarchy fitting v1 injected Omarchy's
own guidance ("read plugins.md first … clone built-in plugins") the moment an agent
touched the bar config. The guidance was correct in general, wrong for a one-field
change, and three models out of three followed it into cloning the whole clock plugin
when the unfitted runs changed `HH:mm` to `HH:mm:ss`. The second thing we learned:
triggers bound to the `edit` tool miss changes made through `bash`, which is how the
agents made them. A rule must match the effect (a path being changed), not the tool.

## 3. Prove it works before stopping

LangChain moved Deep Agents from 52.8% to 66.5% on Terminal-Bench 2.0 by changing only
the harness, and self-verification was one of the main levers: models stop at their
first plausible answer unless the loop makes them check
([LangChain](https://blog.langchain.com/improving-deep-agents-with-harness-engineering)).

h0's fitting names one `verify` check; when the model stops calling tools, the harness
runs it and feeds a failure back, twice at most. A verify gate that already fails on the
untouched tree would trap the agent in breakage it did not cause, so `h0 fit` runs every
check on a clean clone first, and the eval runner re-tests the gate on each task's
starting state.

## 4. Context management decides results under tight budgets

An empirical study of harness design ([arXiv 2609.20804](https://arxiv.org/pdf/2609.20804))
held a loop fixed and varied planning, action space and context management across 176
settings. Context management mattered most when the budget was tight: at 32k tokens,
agents with none overflowed and terminated in 79% of tasks.

That is the local-model case. h0's local profile cuts tool output to 6,000 characters,
clears old tool results at an estimated 24k prompt tokens, and drops `glob`.
[local](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/results/local.md) has the measurements.

## 5. Harness effects depend on the model

The same study found a switch that helped one model cost another 23.2 points. Our
Omarchy runs show a milder version: the fitting left Opus close to vanilla and cost
Haiku the most. A fitting is therefore fitted to a project **and** a model; `h0` records
the model with every result and the learning gate compares like with like.

## 6. Security is built into the harness, not asked for in the prompt

Simon Willison's lethal trifecta and Meta's Agents Rule of Two say the same thing from
two sides: an agent that reads untrusted content, holds private data and can talk to
the outside world will eventually be talked into leaking. A prompt asking the model not
to is not a control.

h0 assumes untrusted input (a repository can carry an injected instruction), so it
keeps the other two apart by construction: commands run with no network, the home
directory hidden, and credentials out of reach; secrets in tool output are redacted;
and the policy refuses any action that would make sensitive data and egress true in the
same session. [security model](../rationale/security-model.md) has the mechanisms and the tests, including
the two holes the tests found on the first night.

## 7. Measured, not claimed

directiveforge commits its measurement spec before any run and its release check
verifies that in git history. Tom Ballard's Omarchy theme evals freeze fixtures,
manifests and scripts with hashes before any session runs, and say plainly which checks
were never observed. h0 follows both: PREREG was committed before the first
eval, every amendment is dated and explained, every task passes an oracle check, and
failed trials are read before they are counted.

Reading the failures paid twice on the first night. The oracle step caught a check that
would have failed the idiomatic `uwsm-app -- nautilus` answer, and reading the learning
loop's diagnosis caught a check that rejected Omarchy's real `o.exec_on_start` function
([eval method](../rationale/eval-method.md)).

## 8. Every component is an assumption about what the model can't do

Anthropic's harness guidance says it directly: each piece of a harness encodes an
assumption about a model's limits, and those assumptions go stale with every model
release. On Omarchy 4, frontier models read `/usr/share/omarchy` themselves and solved
the suite without a fitting. A fitting that repeats what the model will find on disk in
two tool calls is paying rent for nothing. The fitting earns its place where the model
cannot find the truth (private conventions, credentials it must not touch, commands that
hang or reach the network, history only the team remembers).

## 9. Unknown is not zero

A trial that could not be scored is `None`, never a failure. A server that reports no
token usage produces an estimate that says it is one. A verify gate switched off because
the task's starting tree was already broken is recorded on the trial. Every table in
[omarchy](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/results/omarchy.md) carries its unscored count.

## 10. Learning is gated, and the gate has a held-out half

ACE ([arXiv 2510.04618](https://arxiv.org/html/2510.04618v2)) keeps lessons as small
bullets with helpful and harmful counters. Two 2026 memory benchmarks found most memory
setups no better than none, and agents breaking solvable tasks by reusing a lesson
without checking it fit. h0's loop proposes typed edits from paired evidence (what the
fitted run did when it failed, what the vanilla run did when it passed), forbids
task-specific answers, and accepts a candidate only if the training suite does not get
worse; a held-out suite the reflector never saw says whether the change generalised.

Round one passed its gate on the training suite and lost ground for Haiku on the
held-out tasks. The gate needs the held-out half to mean anything.
