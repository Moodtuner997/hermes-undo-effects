"""undo-effects: snapshot what an agent is about to delete or overwrite, so it can be put back.

Registers one ``tool_execution`` middleware (it never blocks, rewrites or approves a tool call),
the ``/undo-effects`` slash command and the ``hermes undo-effects`` CLI command.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from . import commands
from .shellparse import inline_script_risky, parse_command, to_native
from .store import MB, Limits, Store

logger = logging.getLogger(__name__)

_V4A_FILE = re.compile(r"^\*\*\* (Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
_V4A_MOVE = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$", re.M)

_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)\b(\w*(?:key|token|secret|passw(?:or)?d|auth|cred)\w*)=("[^"]*"|'[^']*'|\S+)""")

_state: dict[str, Any] = {"store": None, "config": {}}


def _redact(text: str) -> str:
    """Mask secrets in the command text kept for the timeline (Hermes' own redactor when it is importable)."""
    text = _SECRET_ASSIGNMENT.sub(r"\1=***", text)
    try:
        from agent.redact import redact_sensitive_text

        return str(redact_sensitive_text(text, force=True))
    except Exception:
        return text


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _store() -> Store:
    if _state["store"] is None:
        cfg = _state["config"]
        limits = Limits(
            max_file=int(cfg.get("max_file_mb", 50)) * MB,
            max_entry=int(cfg.get("max_entry_mb", 200)) * MB,
            budget_s=float(cfg.get("snapshot_budget_s", 8)),
        )
        _state["store"] = Store(_hermes_home() / "plugin-data" / "undo-effects", limits)
    return _state["store"]


def _agent_cwd(args: dict[str, Any]) -> str:
    workdir = args.get("workdir")
    if workdir:
        return to_native(str(workdir))
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:
        return os.getcwd()


def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(to_native(path))
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _plan_for(tool: str, args: dict[str, Any]) -> tuple[list[tuple[str, str]], list[str], str]:
    """Return ``(targets, uncovered_reasons, summary)`` for one tool call."""
    if os.environ.get("TERMINAL_ENV", "local") not in ("", "local"):
        return [], [], ""  # file tools and terminal act on a remote sandbox: nothing local to save
    cwd = _agent_cwd(args)
    if tool == "write_file" and args.get("path"):
        return [(_abs(str(args["path"]), cwd), "write")], [], f"write_file {args['path']}"
    if tool == "patch":
        if args.get("mode") == "patch" and args.get("patch"):
            text = str(args["patch"])
            targets = [(_abs(m.group(2), cwd), "delete" if m.group(1) == "Delete" else "write")
                       for m in _V4A_FILE.finditer(text)]
            targets += [(_abs(m.group(1), cwd), "write") for m in _V4A_MOVE.finditer(text)]
            return targets, [], "patch " + ", ".join(Path(p).name for p, _ in targets[:4])
        if args.get("path"):
            return [(_abs(str(args["path"]), cwd), "write")], [], f"patch {args['path']}"
        return [], [], ""
    if tool == "terminal" and args.get("command"):
        command = str(args["command"])
        plan = parse_command(command, cwd)
        if plan.noop:
            return [], [], ""
        summary = "terminal " + _redact(" ".join(command.split())[:400])[:100]
        return [(t.path, t.role) for t in plan.targets], [_redact(u[:200]) for u in plan.uncovered[:20]], summary
    if tool == "execute_code" and inline_script_risky(str(args.get("code", ""))):
        return [], ["execute_code may modify files; not analysed"], "execute_code"
    return [], [], ""


def _on_tool_execution(**kw: Any) -> Any:
    next_call = kw["next_call"]
    entry = None
    try:
        targets, uncovered, summary = _plan_for(str(kw.get("tool_name", "")), kw.get("args") or {})
        if targets or uncovered:
            entry = _store().begin(tool=str(kw["tool_name"]), summary=summary,
                                   session_id=str(kw.get("session_id") or kw.get("task_id") or ""),
                                   targets=targets, uncovered=uncovered)
    except Exception:
        logger.warning("undo-effects: snapshot failed, the tool call runs unprotected", exc_info=True)
    ok = False
    try:
        result = next_call()
        ok = True
        return result
    finally:
        if entry is not None:
            try:
                _store().finish(entry, ok)
            except Exception:
                logger.warning("undo-effects: could not record the result", exc_info=True)


def _slash(raw_args: str = "", session_id: str = "", **_: Any) -> str:
    return commands.run(_store(), raw_args, session_id)


def _cli_setup(parser: Any) -> None:
    import argparse

    # REMAINDER, not "*": flags such as --dry-run and --force belong to the plugin, not to argparse.
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="list | show N | restore N|last [--dry-run] [--force] | prune | doctor")


def _cli_run(args: Any) -> None:
    print(commands.run(_store(), list(getattr(args, "rest", []) or ["list", "all"])))


def register(ctx: Any) -> None:
    try:
        _state["config"] = dict(ctx.get_config() or {})
    except Exception:
        _state["config"] = {}
    _state["store"] = None
    try:
        _store().prune(max_age_days=float(_state["config"].get("retention_days", 14)))
    except Exception:
        logger.debug("undo-effects: startup prune skipped", exc_info=True)
    ctx.register_middleware("tool_execution", _on_tool_execution)
    ctx.register_command("undo-effects", _slash,
                         description="Put back files an agent deleted or overwrote",
                         args_hint="[list|show N|restore N|last|prune|doctor]")
    ctx.register_cli_command("undo-effects", "Put back files an agent deleted or overwrote",
                             _cli_setup, _cli_run)
