---
title: Landscape 2026
tags: [harness-0, research]
updated: 2026-11-08
---

# Landscape 2026

What existed when harness-0 started, and what it says about fitting a harness to one
project. **Verified** means this session opened the original or a search result quoting
it; **reported** means it comes from a secondary summary and was not opened. A ChatGPT
thread supplied many of the leads; a deep-research run
in ChatGPT (GPT-6, High) was started on the same questions and its report is in the
ChatGPT thread linked from 00 Home (its panel renders in a sandboxed frame this
session could not read).

## Harnesses that replace Claude Code

| Harness | What it is | What harness-0 takes |
|---|---|---|
| **Pi** (Mario Zechner, now `earendil-works/pi`) | minimal TypeScript harness: a small core, four tools, JSONL sessions with a tree structure, extension by TypeScript, skills and prompt templates; the harness inside OpenClaw | the small core and the few tools; tree sessions are a next step |
| **Geoffrey Huntley's workshop** (`ghuntley/how-to-build-a-coding-agent`) | a Go workshop that builds the loop, tools and Anthropic integration from scratch | the stance: own the loop rather than configure someone else's |
| **Claude Code, Codex, OpenCode, Crush, Gemini CLI** | the general-purpose harnesses on this machine (all installed via mise) | the reference point the fitting is measured against, through h0's own vanilla arm |

## Precedent for fitting a harness to a project or domain

| Work | What is fitted | How | Evidence | Mark |
|---|---|---|---|---|
| **Meta-Harness** (Lee, Nair, Zhang, Lee, Khattab, Finn; arXiv 2603.28052, Mar 2026) | the harness code itself | a coding agent proposes new harnesses with filesystem access to every prior candidate's code, scores and traces | beats hand-built harnesses on Terminal-Bench 2; one math harness gained 4.7 points across five held-out models | verified |
| **AutoHarness** (Google DeepMind; arXiv 2603.03329, ICLR 2026) | a code harness around the model | the model synthesises action-legality checks or a whole code policy | Gemini 2.5 Flash with its harness beat Gemini 2.5 Pro on TextArena games; 78% of Flash's chess losses had been illegal moves | verified |
| **Harness²** (google-research/harness2, Oct 2026) | prompts, tools, memory, skills | Propose, Probe, Compose over recorded trajectories, model fixed | authors' benchmarks; described as directional | reported |
| **revfactory/harness** (Minho Hwang, Kakao) | Claude Code pre-configuration: agent team, skills, orchestration | a meta-skill generates it per project | 15-task A/B with per-dimension quality scores; numbers from the README | reported |
| **directiveforge** | a project-specific workflow for Claude Code and Cursor | reads the codebase, writes the workflow | publishes a pre-registered report card verified against git history | reported |
| **SemaPLC** (Jul 2026) | a project-grounded, verification-gated harness for PLC code | domain grounding plus verification gates | paper | reported |
| **OpenAI harness engineering** (Lopopolo, Feb 2026) | `AGENTS.md` as a ~100-line map, docs as the system of record, custom linters, background cleanup | hand-built per repository | one product built with no hand-written code | verified |
| **LangChain Deep Agents** (2026) | the harness around gpt-5.2-codex | trace analysis, self-verification, loop detection, context delivery | Terminal-Bench 2.0 from 52.8% to 66.5%, model fixed | verified |
| **Empirical Harness Design** (Fan et al.; arXiv 2609.20804, Sep 2026) | planning, action space, context management | 176 matched settings, one loop | context management decides tight budgets (79% overflow at 32k); a switch that helped one model cost another 23.2 points | verified (summaries) |

What nobody in this table measured is the question harness-0 asks: the same harness, on
the same model, with and without a fitting for one real project, paired task by task.

## Evidence about context, skills and memory

Every claim traced to its original:

- Context files are followed, cost over 20% more, and do not generally raise success
  (arXiv 2602.11988).
- Curated skills lifted pass rates from 33.9% to 50.5%; at most three modules beat a
  bundle (arXiv 2602.12670). Agents loaded every relevant skill in 49% of runs, 31% with
  distractors nearby (arXiv 2604.04323).
- 11 of 12 memory pairings did no better than memory switched off (arXiv 2609.23570);
  reusing a lesson without checking that it fits breaks solvable tasks (arXiv 2610.27524).
- ACE keeps lessons as bullets with helpful and harmful counters (arXiv 2510.04618).

## Security precedent

Simon Willison's lethal trifecta (private data, untrusted content, external
communication); Meta's Agents Rule of Two; CaMeL and the *Design Patterns for Securing
LLM Agents* paper (capability and data-flow approaches); Anthropic's sandbox-runtime and
Codex's Linux sandbox (filesystem and network isolation, the first with a filtering
proxy); the s1ngularity attack on Nx (Aug 2025), which drove installed AI CLIs with
permission-skipping flags; AgentDojo as a way to score utility under attack. How h0
answers each is in [security model](../rationale/security-model.md).

## Omarchy agent tooling

- **Omarchy's own skill** ships in the package at
  `/usr/share/omarchy/default/agents/skills/omarchy/` (SKILL.md plus guides for Hyprland,
  plugins, theming, hooks, capture, contributing) and a `diagnose-crash` skill. Omarchy 4
  links it into the skill folders of Claude Code, Codex, Pi and `~/.agents/skills`.
- **Tom Ballard's** `build-omarchy-themes` (twelve skills, MIT) carries a frozen,
  pre-registered evaluation protocol that has not yet run for want of a host;
  `build-omarchy-plugins` is its sibling for shell plugins.
- The Omarchy 4 bar has an agents panel and a plugin kind for agents
  ([omarchy 4](https://github.com/parker-brown-family/omarchy-fitted-harness/blob/main/docs/research/omarchy-4.md)).
