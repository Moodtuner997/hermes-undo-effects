"""Text interface shared by ``/undo-effects`` (in a session) and ``hermes undo-effects`` (shell)."""

from __future__ import annotations

import os
import shlex
import time
from typing import Any

from .store import Store

HELP = (
    "undo-effects: put back files an agent deleted or overwrote.\n"
    "  list [all]                      recent effects (this session; `all` for every session)\n"
    "  show N                          what one effect touched\n"
    "  restore N|last [--dry-run] [--force]   put the files back (N is the number from `list`)\n"
    "  prune                           delete old snapshots\n"
    "  doctor                          check the install and the store\n"
    "A restore never overwrites a file edited after the agent's change unless you pass --force,\n"
    "and it keeps a safety copy of whatever it replaces, so it can itself be restored."
)

SAFETY_TOOL = "undo-effects.restore"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _count(node: dict[str, Any]) -> tuple[int, int]:
    if node["kind"] == "file":
        return 1, node["size"]
    files = size = 0
    for child in node.get("children", {}).values():
        f, s = _count(child)
        files += f
        size += s
    return files, size


def _describe(entry: dict[str, Any]) -> str:
    saved_files = saved_bytes = 0
    for item in entry["items"]:
        f, s = _count(item["pre"])
        saved_files += f
        saved_bytes += s
    parts = []
    if saved_files:
        parts.append(f"{saved_files} file(s), {_fmt_size(saved_bytes)} saved")
    created = sum(1 for i in entry["items"] if i["pre"]["kind"] == "absent")
    if created:
        parts.append(f"{created} new path(s)")
    if entry["uncovered"]:
        parts.append(f"not covered: {entry['uncovered'][0]}" + (" +more" if len(entry["uncovered"]) > 1 else ""))
    if entry.get("restored"):
        parts.append("restored")
    return "; ".join(parts) or "nothing to restore"


def _scope(store: Store, session_id: str, show_all: bool) -> list[dict[str, Any]]:
    """Recent effects, newest first, without the safety copies restores leave behind."""
    entries = store.list(session_id=None if show_all or not session_id else session_id, limit=500)
    return [e for e in entries if e["tool"] != SAFETY_TOOL][:25]


def _pick(store: Store, entries: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    if token == "last":
        return next((e for e in entries if e["items"] and not e.get("restored")), None)
    if token.isdigit() and 1 <= int(token) <= len(entries):
        return entries[int(token) - 1]
    try:  # a full id, which is also how a safety copy is addressed
        return store.load(token)
    except (OSError, ValueError):
        return None


def _list(store: Store, session_id: str, show_all: bool) -> str:
    entries = _scope(store, session_id, show_all)
    if not entries:
        where = "in any session" if show_all or not session_id else "in this session (try `list all`)"
        return f"No recorded effects {where}."
    lines = [f"undo-effects: {len(entries)} recent effect(s), newest first"]
    for i, e in enumerate(entries, 1):
        when = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        lines.append(f"{i:>3}  {when}  {e['tool']:<14} {e['summary'][:70]}")
        lines.append(f"       {_describe(e)}")
    lines.append("Restore with `restore N` (add --dry-run to preview).")
    return "\n".join(lines)


def _show(store: Store, entries: list[dict[str, Any]], token: str) -> str:
    entry = _pick(store, entries, token)
    if not entry:
        return f"No effect {token!r}. Run `list` first."
    lines = [f"{entry['id']}  {entry['tool']}  {entry['summary']}"]
    for item in entry["items"]:
        files, size = _count(item["pre"])
        lines.append(f"  {item['role']:<7} {item['pre']['kind']:<8} {item['path']}  ({files} file(s), {_fmt_size(size)})")
    for note in entry["uncovered"]:
        lines.append(f"  not covered: {note}")
    return "\n".join(lines)


def _restore(store: Store, entries: list[dict[str, Any]], tokens: list[str], session_id: str) -> str:
    flags = {t for t in tokens if t.startswith("--")}
    positional = [t for t in tokens if not t.startswith("--")]
    if not positional:
        return "Which effect? Use `restore N` or `restore last` (see `list`)."
    entry = _pick(store, entries, positional[0])
    if not entry:
        return f"No effect {positional[0]!r}. Run `list` first."
    if not entry["items"]:
        return "That effect has nothing to restore: " + _describe(entry)
    report = store.restore(entry["id"], force="--force" in flags, dry_run="--dry-run" in flags,
                           session_id=session_id)
    verb = "Would restore" if report["dry_run"] else "Restored"
    lines = [f"{verb} {len(report['restored'])} path(s) from {entry['id']}."]
    lines += [f"  {p}" for p in report["restored"][:15]]
    if len(report["restored"]) > 15:
        lines.append(f"  ... and {len(report['restored']) - 15} more")
    if report["unchanged"]:
        lines.append(f"Already in the original state: {len(report['unchanged'])} path(s).")
    for p in report["conflicts"]:
        lines.append(f"Skipped (changed since the agent's action; use --force to overwrite): {p}")
    for p in report["unrestorable"]:
        lines.append(f"Could not be saved at the time: {p}")
    if report.get("safety_entry"):
        lines.append(f"What was replaced is kept: `restore {report['safety_entry']}` puts it back (redo).")
    return "\n".join(lines)


def _doctor(store: Store) -> str:
    backend = os.environ.get("TERMINAL_ENV", "local") or "local"
    entries = store.list(limit=10**9)
    lines = [
        f"store: {store.root}",
        f"entries: {len(entries)}, size on disk: {_fmt_size(store.total_size())}",
        f"limits: file {store.limits.max_file // (1024 * 1024)} MB, entry {store.limits.max_entry // (1024 * 1024)} MB, "
        f"snapshot time budget {store.limits.budget_s:.0f} s",
        f"terminal backend: {backend}" + ("" if backend == "local" else
                                          "  (WARNING: snapshots only cover this machine's disk)"),
    ]
    try:
        store.root.mkdir(parents=True, exist_ok=True)
        probe = store.root / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        lines.append("store writable: yes")
    except OSError as exc:
        lines.append(f"store writable: NO ({exc})")
    return "\n".join(lines)


def run(store: Store, raw: str | list[str], session_id: str = "") -> str:
    try:
        tokens = shlex.split(raw) if isinstance(raw, str) else list(raw)
    except ValueError:
        return HELP
    if not tokens or tokens[0] in ("help", "-h", "--help"):
        return HELP if tokens else _list(store, session_id, False) + "\n\n" + "(`help` for all commands)"
    cmd, rest = tokens[0], tokens[1:]
    show_all = "all" in rest
    if cmd == "list":
        return _list(store, session_id, show_all)
    if cmd == "last":
        return _restore(store, _scope(store, session_id, show_all), ["last"] + rest, session_id)
    if cmd == "show":
        return _show(store, _scope(store, session_id, show_all), rest[0] if rest else "1")
    if cmd == "restore":
        return _restore(store, _scope(store, session_id, "--all" in rest), [t for t in rest if t != "--all"],
                        session_id)
    if cmd == "prune":
        res = store.prune()
        return f"Removed {res['entries_removed']} old entries, freed {_fmt_size(res['bytes_freed'])}."
    if cmd == "doctor":
        return _doctor(store)
    return HELP
