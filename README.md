# undo-effects

**Put back what your agent deleted or overwrote.**

Agents make mistakes that no `/undo` of the conversation can fix: an `rm -rf` in the wrong folder, a `write_file`
over a file that was never committed, a `sed -i` that ate a config. `undo-effects` saves the affected files just
before the tool runs, then lets you list the effects and restore them with one command.

```text
> /undo-effects list
undo-effects: 3 recent effect(s), newest first
  1  14:07:12  terminal       terminal rm -rf build/ dist/
       2 file(s), 1.2 MB saved
  2  14:05:40  write_file     write_file src/config.py
       1 file(s), 2.1 KB saved
  3  14:02:03  terminal       terminal git clean -fd
       not covered: git clean rewrites the work tree

> /undo-effects restore 1
Restored 2 path(s) from 20261104-140712-3bee.
What was replaced is kept: `restore 20261104-140731-91ac` puts it back (redo).
```

## What it does

- **Saves before, never interferes.** One `tool_execution` middleware snapshots the targets and then runs the
  tool unchanged. It does not block, rewrite, approve or auto-approve anything, so Hermes' approval gate and
  guardrails see exactly the call the model made.
- **Works outside git.** Scratch folders, notes, dotfiles, files created by the agent, directories: all covered
  by a content-addressed store under `HERMES_HOME/plugin-data/undo-effects/`.
- **Restores safely.** A restore refuses to overwrite a file that was edited after the agent's change (use
  `--force` to override), never deletes content it could not snapshot, and keeps a safety copy of whatever it
  replaces, so a restore can itself be undone.
- **Says what it cannot cover.** Commands it cannot analyse show up in the timeline as *not covered* instead
  of silently doing nothing.

### Coverage

| Tool | Covered |
|---|---|
| `write_file`, `patch` (replace and multi-file V4A patches) | the files written, moved or deleted |
| `terminal` | `rm`, `rmdir`, `unlink`, `shred`, every `mv` (renames included), `cp`/`install` onto a path, `>` and `>>`, `tee`, `sed -i`, `truncate`, `dd of=`, `find ... -delete`, through `sudo`, `env`, `bash -c`, pipes and `&&` chains, with globs, braces and `~` expanded |
| reported as *not covered* | `git clean`, `git reset --hard`, `git checkout --`, `git restore`, `find -exec`, `xargs rm`, command substitution, heredocs, `eval`, PowerShell and `cmd` commands, inline `python -c`/`node -e` that write files, `execute_code` that looks like it writes |
| not saved, listed as such | files named like credentials (`.env`, `*.pem`, `id_*`, `auth.json`, ...), files above 50 MB, `node_modules`, `.venv`, `.git` |
| out of scope in v1 | messages sent, tickets created, MCP side effects (an inverse-action registry is the v2 roadmap) |

## Install

```bash
hermes plugins install undo-effects
hermes plugins enable undo-effects
```

## Use

In a session: `/undo-effects` (or `list`, `list all`, `show N`, `restore N`, `restore last`, `restore N --dry-run`,
`restore N --force`, `prune`, `doctor`). From a shell: `hermes undo-effects <same arguments>`.

Settings (`config_schema`): largest file (50 MB), largest snapshot per tool call (200 MB), retention (14 days),
snapshot time budget (8 s; a snapshot that would take longer is cut short and reported, and the tool still runs).

## Disclosure

- **Network:** none.
- **Reads outside the plugin's own data:** the contents of the files and directories a covered tool call is about
  to change, and their metadata.
- **Writes:** unencrypted copies of those files under `HERMES_HOME/plugin-data/undo-effects/` (objects, entries).
  Files whose names look like credentials are skipped, but a secret stored under an ordinary name would be copied:
  set a short `retention_days` on shared machines.
- **Shell commands, background processes, credentials:** none. The command is only parsed, never executed.
- **Failure mode:** if a snapshot fails the tool call runs anyway, with a warning in the log.
- **Terminal backends:** only the local backend is covered. With `TERMINAL_ENV` set to a remote sandbox the plugin
  does nothing (`doctor` warns).
- **Telemetry:** none.

## How it differs from `git-autosave` and `agent-ledger`

`git-autosave` backs up Git working trees to shadow refs and is restored with its own CLI; it supports Linux and
macOS and observes only. `agent-ledger` journals side effects but does not restore them. `undo-effects` is not tied
to Git, snapshots at execution time through the middleware seam, restores from the session with `/undo-effects`,
guards against clobbering later edits, makes restores reversible, and reports what it could not cover. They compose:
use them together if you like.

## Development

```bash
python -m pytest tests -q                 # 44 tests, no Hermes needed
hermes plugins validate .                 # the catalog gate
HERMES_HOME=/tmp/hk/home <hermes python> tests/e2e_hermes.py   # real tool calls through Hermes, no LLM
```

Tested on Hermes 0.22.7: Linux (arm64, Ubuntu) and native Windows 11 (Git for Windows bash), unit tests, `hermes plugins
validate`, the end-to-end script, and one real agent turn on each. macOS is untested.

Known limits: v1 saves files, not other side effects; commands the parser does not understand are reported, not
guessed; a file changed by a *background* process between snapshot and restore is treated like any other later edit.

MIT licence.
