---
title: Using h0
tags: [harness-0, guide]
updated: 2026-11-08
---

# Using h0

`h0` is on `PATH` whenever a shell sits in this repository (agent shells here put the
working directory's `bin/` on `PATH`). From anywhere else, call the launcher directly.

```bash
bin/h0 doctor
```

`doctor` checks Python, bubblewrap, that the sandbox really hides the home directory and
the network, that the API key is reachable, and which fitting applies to the current
directory.

## The key

The Anthropic key lives in the GNOME keyring and nowhere else.

```bash
secret-tool lookup service harness-0 account anthropic-api >/dev/null && echo present
```

To replace it, run the store command and paste the new key at its prompt (it is read from
the terminal, never from the clipboard).

```bash
secret-tool store --label="harness-0 Anthropic API key" service harness-0 account anthropic-api
```

`ANTHROPIC_API_KEY` in the environment overrides the keyring.

## A session

Interactive, in the project you want to work on:

```bash
cd ~/code/my-project && bin/h0 --model claude-opus-5-5 --effort high
```

One task, headless, with a JSON summary at the end:

```bash
bin/h0 -p "Make the failing test pass" --model claude-sonnet-5-5 --json
```

Inside a session: `/cost`, `/model <id>`, `/effort <level>`, `/trace`, `/quit`. Risky
commands (destructive git, deleting a tree, network) ask first; `--yolo` allows them inside
the sandbox. `--plan` is read-only.

The fitting is picked by path: the fitting whose `root` is the longest prefix of the
current directory. `--fitting <name>` chooses one; `--no-fitting` runs the bare harness.

## A local model

Any OpenAI-compatible server works. For ollama, a model tag with a context window large
enough for a harness transcript (the default is about 4k tokens) was created tonight as
`qwen3.5-9b-32k`.

```bash
bin/h0 --provider openai --base-url http://127.0.0.1:11434/v1 --model qwen3.5-9b-32k
```

The local profile (cut tool output, earlier context clearing, five tools) turns on by
itself for a local provider.

## Building a fitting

```bash
bin/h0 fit ~/Work/some-project --name some-project
```

One Opus call over a static survey and the project's own instruction files (add more with
`--doc <file>`, and a paragraph of context with `--brief`), then every proposed check is run
once on a clean clone. The fitting lands in `~/.local/share/h0/fittings/<name>/` unless
`--out` says otherwise. Read `map.md` before trusting it.

## Measuring it

```bash
bin/h0 eval mine ~/Work/some-project --project sp --test-cmd "npx vitest run {tests}" --link node_modules --out evals/sp/tasks.jsonl
```

```bash
bin/h0 eval run evals/sp/tasks.jsonl --arms vanilla,fitted=some-project --models claude-haiku-5-5,claude-sonnet-5-5 --trials 2 --out runs/sp-v1
```

`h0 eval validate` oracle-checks a suite, `h0 eval report <run>` prints the paired tables,
`h0 eval rescore` re-scores old trials by replaying their tool calls after a check is
corrected.

## Improving it

```bash
bin/h0 learn propose some-project --runs runs/sp-v1 --suite evals/sp/tasks.jsonl --out fittings/some-project-v2 --name some-project-v2
```

Then run the candidate and gate it against the previous version:

```bash
bin/h0 learn gate --prev runs/sp-v1 --cand runs/sp-v2 --cand-arm fitted-v2
```

## Traces

Every session writes a hash-chained trace to `~/.local/state/h0/sessions/`.

```bash
bin/h0 trace verify ~/.local/state/h0/sessions/<file>.jsonl
```

## Omarchy

Audit a theme before switching to it:

```bash
bin/h0 omarchy audit-theme ~/.config/omarchy/themes/<name>
```

Serve as the evaluation host for Tom Ballard's theme protocol: `h0 host` reads one JSON
request on stdin and writes the contract's JSONL to stdout.
