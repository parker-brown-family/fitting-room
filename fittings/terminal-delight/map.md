# Terminal Delight (TD)
GPU-native Linux terminal (X11/Wayland only) with tabs and tiling splits, hot-reload TOML themes, per-pane CRT warp, sticky notes, an agent wall, tab badges, a read-only MCP server and subscription usage. It watches Claude and Codex panes and is not an orchestrator. Outside contributors send PRs.

## Layout
- `app/`: the one big Rust crate. Everything is here and nothing builds cheaply.
  - `src/main.rs`: workspace (tabs, tiling tree, popups, wall, persistence, MCP wiring). Hottest file.
  - `src/pane.rs`, `src/pane/`: per-pane TerminalView (paint, input, selection, links, header).
    - `pane/harness.rs`: gpui headless test harness. `pane/bench.rs`, `pane/behaviour.rs`.
  - `src/vt/`: the **only** place allowed to name `rio_vt` or `alacritty_terminal`.
    - Files: `pump.rs` read loop, `pty.rs`, `socket.rs`, `rio.rs`, `alacritty.rs`, `kitty.rs`/`text.rs`/`compat.rs` parser guards, `types.rs`.
  - `src/term.rs`: a pane's terminal session. `src/session.rs`, `src/recover.rs`: cwd and agent resume.
  - `src/theme.rs`: themes plus the `BUILTIN_THEMES` registry. `src/warp.rs`, `src/crt.rs`: CRT and barrel warp.
  - `src/mcp.rs`, `mcp_transport.rs`, `mcp_tail.rs`: MCP server (list_panes, pane_events, get/set_pane_config, grep).
  - `src/docview/`: in-pane document and Markdown viewer.
    - `markdown.rs` is lifted from markdown-delight; keep it compiling unchanged.
  - `src/lang.rs`: all UI strings, in nine languages. `src/usage.rs`, `src/vitals.rs`: agent subscription usage.
  - `src/vendor/`: vendored upstream code (e.g. the Grok usage collector), kept **unedited**.
  - `tests/`: integration tests (`snapshot_engine.rs`, `host_socket.rs`, `workbench_surface.rs`, ...), plus `tests/fixtures/`.
  - `themes/*.toml`, `skins/`, `palettes/`, `assets/`: data files.
  - `deny.toml`: no GPL exceptions. `about.toml`: generates `THIRD-PARTY-LICENSES.md`.
- `../../zed-upstream/` (= `~/Work/zed-upstream`): pinned Zed checkout supplying gpui, with the `docs/patches/*.patch` stack applied. **Read-only. Never edit it.**
- `docs/`
  - `PLAN.md`: plan, gates, and the §2 clean-room rule.
  - `features/`: catalog, plus `ENV-FLAGS.md`.
  - `plans/`: per-feature plans and status files, e.g. `core-swap-rio/`.
  - `decisions/`: ADRs. `security/terminal-input.md`. `patches/`: gpui patches.
- `docsite/` (`build.mjs`, `pages/`), root `*.html`, `assets/`, `scripts/verify-site.mjs`: website.
- `src/js`, `src/styles`, `index.html`: original browser prototype (design reference). `web-warp-lab/`: experiments.
- `reports/` (649 files), `handoffs/`, `docs/handoffs/`, `CEREMONIES/`: archives. Exclude them from broad greps.
- `.worktrees/`, `.claude/worktrees/`: **other git worktrees (~100 total). Never read-modify-write, checkout, or delete them or their branches.**
- `scripts/`: setup-deps, prepare-gpui, build-appimage, release-smoke, land.sh. All need network or are slow; do not run them.

## Build and test (always targeted, always `--offline`)
A full build is very expensive: the target dir is 46 GB. The harness sets `CARGO_TARGET_DIR` to a shared cache outside the repo. **Never `cargo clean`, never delete target dirs, never override CARGO_TARGET_DIR.**
```bash
cd app
cargo check --offline                                    # default: rio-vt core
cargo check --offline --features core-alacritty          # fallback/oracle core
cargo test --offline --lib <module::path_or_name>        # always give a filter
cargo test --offline --lib vt::boundary                  # core-crate isolation guard
cargo test --offline --features core-alacritty --lib <filter>  # when touching src/vt/
cargo clippy --offline -- -D warnings                    # CI: warnings are errors
cargo fmt --check
```
- Do not run `cargo build`, `cargo run`, `cargo build --release`, an unfiltered `cargo test`, or `cargo deny` (deny fetches advisories). CI does those.
- CI pins Rust 1.98.0.
- Web side: `node --check src/js/*.js` from the root. `docsite/build.mjs` and `scripts/verify-site.mjs` serve the site.

## Invariants
- **Core boundary:** anything reading a terminal goes through `crate::vt`. No file outside `app/src/vt/` may name `rio_vt` or `alacritty_terminal`; `vt::boundary::no_file_outside_vt_names_a_core_crate` fails otherwise. Seam changes must pass on both cores.
- **Clean-room Zed:** Zed's `terminal`/`terminal_view` are GPL-3.0. "study only, never copy": no transcribed bodies, identifiers or structure.
- **License:** TD source and binaries are MIT. `cargo deny` allows no GPL. Do not add dependencies; the sandbox is offline, and every dep in `Cargo.toml` carries a why-comment.
- **PTY writes:** every path that can write to a pty is enumerated in `docs/security/terminal-input.md`. A new write path must be added there. MCP is appearance-only and must never write bytes to a PTY.
- **Themes:** `name` must equal the registry id in `BUILTIN_THEMES` (one `(id, include_str!(...))` line); a test enforces this. `ansi` has exactly 16 entries. All `[effects]` are optional, and 0 = off.
- **UI strings** go through `src/lang.rs`, never hard-coded.
- **Hot paths:** `pane.rs::render` runs per frame. No filesystem walks, image loads or scans in render paths.
- **Privacy:** `~/.config/terminal-delight/state.toml` is written 0600. Screenshots use staged demo content, never a real shell.
- **Tests:** gpui-driven tests use `TestAppContext` via `pane/harness.rs`. Async and lock assertions wait for the event to land through a helper, never assert immediately (past flake fixes).

## Conventions
- Commit subjects are plain-English sentences about behaviour. Example: "A paste while a sticky note ... goes into it, not into the shell under it".
- User-visible changes get a `CHANGELOG.md` entry. Feature plans keep a status file in `docs/plans/<feature>/` updated as built.
- Write new code in the idiom of the surrounding file. Comments explain why.
- Release work: 0.5.0 targets the `dev/0.5.0` branch (separate worktree `~/Work/td-dev-0.5.0`, do not touch). Stay on the current branch; do not switch, rebase or merge branches.
