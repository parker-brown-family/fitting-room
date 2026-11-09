# fitting-room

`h0` is a coding-agent harness you can fit to your own project. It runs a model directly
(the Anthropic Messages API, or any OpenAI-compatible server such as ollama) in place of
Claude Code or Codex, and it is written in Python's standard library with no dependencies.

A **fitting** is a small bundle that tailors the harness to one project: a short map of the
project, named checks, rules that fire before or after a matching tool call, sandbox policy,
and lessons. This repository is the fitting room: the harness and the three tools that make
a fitting worth having.

- `h0 fit <repo>` surveys a project and writes a fitting.
- `h0 eval` runs the same tasks with and without the fitting, on the same model, and reports
  paired differences in success, cost and time.
- `h0 learn` reads failed fitted runs beside passing unfitted ones, proposes typed edits to the
  fitting, and keeps them only if an eval says so.

## Quick start

```bash
git clone https://github.com/parker-brown-family/fitting-room && cd fitting-room
bin/h0 doctor
```
```bash
export ANTHROPIC_API_KEY=...   # or store it in the GNOME keyring, see docs/guides/using-h0.md
bin/h0 fit ~/code/my-project --name my-project
cd ~/code/my-project && ~/fitting-room/bin/h0
```

A local model works the same way:

```bash
bin/h0 --provider openai --base-url http://127.0.0.1:11434/v1 --model qwen3.5-9b-32k
```

## Security

Every command the model runs goes through bubblewrap: the home directory is hidden, there is
no network unless a command is granted it, `/run` is masked, git hooks and config are
read-only, and credential files are masked. Secrets in tool output are redacted before the
model sees them. A session may never hold sensitive data and network access at once. Every
session is written as a hash-chained trace you can verify with `h0 trace verify`.

An optional judge reads any shell command the rules do not cover and asks you before running
one that looks like bulk deletion, sending data away, reading credentials or leaving something
running. It uses Jev (TypeSafe's System One model) through OpenRouter and is only active when
its client is installed; when it is, the redacted command text is sent to OpenRouter.
`H0_JUDGE=off` turns it off.

## What we measured

The question behind h0 is whether a harness should be fitted to the project it works on. The
first answers, from paired and pre-registered evals:

- **A fitting pays most where the model is weakest.** On Omarchy configuration tasks a 9B model
  running locally needed a quarter of the turns and a fifth of the prompt fitted, and passed 73%
  against 59% unfitted (22 pairs; that success gap is not yet significant).
- **Frontier models were at the ceiling on our suites**, fitted or not, so for them a fitting
  changed only the cost.
- **A generated fitting can make things worse.** The first Omarchy fitting relayed advice from
  Omarchy's own documentation that sent every Claude model the long way round; two rounds of
  `h0 learn` fixed it. That story, the Omarchy fitting and its suites are in
  [omarchy-fitted-harness](https://github.com/parker-brown-family/omarchy-fitted-harness).

Evaluating a harness turned out to need the same isolation the harness gives its agents. Two
of our own eval defects are written up in [docs/rationale/eval-method.md](docs/rationale/eval-method.md):
trials that shared a build directory ran each other's programs, and mined tasks whose
workspace kept git history let agents read the answer. Both are fixed and tested.

## Layout

- `h0/` the harness: loop, tools, sandbox, policy, redaction, trace, providers, fit, eval, learn.
- `tests/` run with `python3 -P -m unittest discover -s tests -t .` (no network, no key).
- `fittings/` example fittings: h0 fitted to itself, and to Terminal Delight, a public Rust terminal.
- `evals/` the Terminal Delight suite, the judge's labelled commands, and the symptom-variant tool.
- `docs/` why it is built this way, how to use it, and what the runs found.

## Credits

h0 learned from Pi (Mario Zechner), pstack (Cursor), Tom Ballard's build-omarchy-plugins and
build-omarchy-themes, whose eval host contract `h0 host` implements, and the public
writing listed in [docs/research/landscape-2026.md](docs/research/landscape-2026.md).

MIT licence.
