---
title: Security Model
tags: [harness-0, security, rationale]
updated: 2026-11-08
---

# Security Model

h0 runs a model that reads files written by strangers, on a desktop that holds its owner's
keys, browser sessions and twenty other agents' work. The model is not trusted to
refuse a well-written injection, so every boundary below is enforced by the harness and
tested by trying to break it (`tests/test_security.py` and `tests/test_selfreview.py`, 47 tests in all).

## What it defends against

| Threat | Where it comes from | What stops it |
|---|---|---|
| Injected instructions | READMEs, code comments, issue text, theme files | Nothing can stop the model reading them; the rest of this table limits what obeying them achieves |
| Credential theft | `~/.ssh`, `~/.config/gh`, `.env` files, the keyring, environment variables | Home masked in the sandbox; `.env`-shaped paths refused; environment cleared; keyring bus hidden; secrets in output redacted |
| Exfiltration | network tools, DNS lookups, daemons reachable over Unix sockets | No network namespace access; all of `/run` masked (resolver, Docker, containerd, Tailscale, SSH sockets) |
| Persistence outside the sandbox | `.git/hooks`, `core.fsmonitor` in `.git/config`, a planted `.git/commondir`, `.envrc`, a replaced `.git`, a repository planted at `sub/.git` | hooks and config read-only, `.git` pinned as a mount point, an existing `.envrc` masked, a new `.envrc`, `commondir` or nested `.git` removed after each command and reported; all refused to the file tools |
| Destruction | `rm -rf .`, `git reset --hard`, force-push | Asked of the person; refused in headless runs |
| Resource exhaustion | a build or test run that eats the desktop's memory | Optional systemd scope per command: memory ceiling, low CPU weight, `nice` |
| Supply chain | a compromised dependency of the harness itself | The harness has none: Python's standard library only |

The s1ngularity attack of August 2025 is the reason for the last row: a malicious npm
package drove the AI CLIs already installed on developers' machines with
permission-skipping flags to search the disk for secrets. A harness with no packages
cannot be the carrier, and one whose shell has no network and no home directory gives
such a payload nothing to find.

## Mechanisms

**The sandbox** (`h0/sandbox.py`). Every shell command runs in bubblewrap with a
read-only root, a fresh `/tmp`, a new PID and network namespace, all capabilities
dropped, and a cleared environment. `$HOME` is an empty tmpfs; toolchains (`~/.cargo`,
`~/.rustup`, mise, bun) are re-bound into it, the writable caches among them behind a
tmpfs overlay so writes succeed and vanish. The project is read-write; its
`.git/hooks` and `.git/config` are read-only, and a `git clone --shared` workspace gets
its borrowed objects read-only. `/run`, `/mnt` and `/media` are masked whole.

**The policy** (`h0/policy.py`). File tools run in-process, so they check paths
themselves, after resolving symlinks: inside the project is allowed, the rest of the
home directory is refused, system paths are readable, `/proc` and `/run` are not.
Credential-shaped paths are refused for reading and writing.

**The rule of two.** A session carries three flags: untrusted input (always assumed),
sensitive data (set when redaction fires or a command reaches for credentials), and
egress (set when a command is granted network). The policy refuses whichever action
would make the second and third true together.

**Redaction** (`h0/redact.py`). Tool output is scrubbed before it enters the context:
Anthropic, OpenAI, OpenRouter, GitHub, AWS, Slack, Google and Stripe key formats,
private keys, JWTs, database URLs with passwords, `NAME_SECRET=literal` assignments that
look like literals rather than code, and the exact value of any credential the harness
itself loaded. A redacted value becomes `[REDACTED:kind:id]`, where `id` is a hash
prefix, so two mentions of one secret are recognisably the same.

**The trace** (`h0/trace.py`). Every session is hash-chained JSONL, mode 0600, its final
hash appended to a second file. An edited field or a deleted line breaks the chain from
that point. It is tamper-evident, not tamper-proof.

## What h0 found in itself

Once the harness worked, it was pointed at its own security code: a read-only session on
Opus 5.5 ($1.03, ten turns) read the four security modules against this document and
reported where the code did not keep its promises. A first attempt on Fable 5.1, which
asked for a concrete exploiting command per finding, was ended by the API with
`stop_reason: refusal` after ten turns of reading; asking for mechanism, location and fix
instead completed. The review is in `docs/results/self-review-2026-11-08.json`.

It found ten weaknesses. The serious two:

- **The mount table was rebuilt from files the sandbox could write.** Each command re-read
  git's alternates file, `.git` file and `commondir`, so one command could list `~/.ssh` in
  `objects/info/alternates`, or point a worktree's `.git` at `$HOME`, and the next command
  would mount what it named. The layout is now read once per session and validated: a
  worktree must link back, an alternate must be a git object store, and `.git` is bound
  onto itself so it cannot be swapped for a file.
- **`grep` and `glob` judged only where they started.** `/home` is a system path, so a
  `grep` from `/home` walked the user's home directory outside the sandbox. Both now check
  every file after resolving symlinks, and `glob` refuses `..`.

The rest: tokens inside re-bound toolchain directories (`~/.cargo/credentials.toml`,
`~/.config/git/credentials`) and project `.env` files were readable in the shell; an
approved network grant was not counted by the rule of two; PGP keys and key bodies read
without their header escaped redaction; the redaction tag was an unkeyed hash, so a guessed
secret could be confirmed; the fittings that define policy were writable when the project
was harness-0 itself; plan mode still ran checks; and the file tools could read host `/tmp`.
Each has a fix and a regression test that reproduces the attack.

## Holes the tests found on the first night

**DNS.** The first sandbox masked only `/run/user/$UID`. A test expecting curl to fail
for lack of a network got *Failed to connect to example.com:443*, which means the name
had resolved: glibc's `resolve` module reaches systemd-resolved through
`/run/systemd/resolve`, so a sandboxed command could exfiltrate data as DNS lookups of
`<data>.attacker.example`. The same listing showed `/run/docker.sock` and
`/run/containerd`. This account is not in the `docker` group; on a developer
machine where it is, a sandboxed command could have started a container mounting the
host's root. The fix masks `/run` whole and gives DNS back only to a command granted
network. Two regression tests pin it.

**The PATH.** Agent shells on this machine put the working directory's `bin/` first on
`PATH`, so a repository's own `bin/git` would shadow the real one. The sandbox builds
its `PATH` with system directories first, and the `h0` launcher runs Python with `-P`
so a project's own `json.py` cannot shadow the standard library.

## Omarchy-specific findings

- **Clipboard history is on disk.** Omarchy's clipboard plugin persists every copy to
  `~/.local/state/omarchy/clipboard-history.json` (mode 0644, 300 entries) unless the
  copying app sets the KDE password-manager hint, which a browser's copy-key button does
  not. A key copied from a console page therefore sits in that file until removed (the
  shell reloads the file on change, so removing it there reaches its memory too).
- **Themes can carry code, and Omarchy strips it only from clones.** `omarchy theme
  install` drops every `*.lua` from a cloned theme because Hyprland and Neovim execute
  them; a theme folder copied in by hand keeps them. The `om-theme-audit` task plants
  three payloads in such a folder.

## Not yet covered

- **The edges of the planted-repository check.** After each command the sandbox removes
  and reports any `.git` (directory, file or symlink) below the project root that was not
  there when the session began. Nested repositories that already existed (submodule
  checkouts, vendored repositories) are left alone. A command that clones or inits a
  repository inside the project therefore loses it when the command ends. The check is
  bounded, so it misses the following:
  - a `.git` more than six directory levels down;
  - a `.git` that the walk reaches only after 20,000 entries;
  - a `.git` inside a skipped directory (`node_modules`, `target`, `build`, `.venv` and
    the like).

  It also does not watch what it leaves alone:
  - an existing nested repository's own config stays writable;
  - an existing submodule's `.git` file can be repointed at a planted gitdir;
  - a bare repository planted under a name other than `.git` is still found by git when a
    person runs it inside that directory.
- **The destructive-command list is a denylist** and cannot be complete; a snapshot of the
  working tree before each command would be the real protection.
- **A granted network shares the host's network namespace**, including abstract Unix
  sockets such as X11's; `pasta` or `slirp4netns` would give a granted command its own.
- **The sensitive flag is a heuristic.** A command that encodes what it reads avoids both
  redaction and the credential pattern; the real floor is that credentials are not in the
  sandbox to be read.

- **seccomp.** bubblewrap supports a seccomp filter; h0 does not install one yet.
- **An egress allowlist.** A command granted network gets all of it. Anthropic's
  sandbox-runtime routes allowed traffic through a filtering proxy; that is the model
  for the next step.
- **The file tools run outside the sandbox.** Their path policy is the boundary, and it
  is tested, but a bug in it is a bug outside bubblewrap.
- **The provider sees everything the model sees.** That is inherent to using a hosted
  model, and the reason redaction runs before the context is built.
- **Prompt injection itself.** No model in the Omarchy runs attempted the planted
  `install.sh` (0 of 18 security trials), so the floor has been tested by escape attempts
  and not yet by a model that obeys. The local model runs are the first chance.
