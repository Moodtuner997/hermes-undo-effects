"""Work out which files a shell command is about to delete or overwrite.

The parser is deliberately narrow: it understands the common destructive verbs and
says so when it cannot (``Plan.uncovered``), instead of guessing.  It never executes
anything and never touches the disk except to expand globs and test existence.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from dataclasses import dataclass, field

MAX_DEPTH = 3

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&"}
_WRAPPERS = {"sudo", "command", "builtin", "nohup", "time", "nice", "doas", "stdbuf"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_INLINE_INTERPRETERS = {"python", "python3", "node", "perl", "ruby"}
_INLINE_DANGER = re.compile(
    r"remove|rmtree|unlink|rmdir|rename|replace|truncate|write|open\(|fs\.|File\.|shutil", re.I
)
_FOREIGN_SHELLS = {"powershell", "pwsh", "cmd", "cmd.exe", "powershell.exe", "pwsh.exe"}
_WINDOWS_ONLY = {"remove-item", "del", "erase", "rd", "move-item", "set-content", "out-file"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")
_IGNORED_PREFIXES = ("/dev/", "/proc/", "/sys/")


def inline_script_risky(text: str) -> bool:
    """True when inline code (``execute_code``) looks like it can change files."""
    return bool(_INLINE_DANGER.search(text))


@dataclass
class Target:
    path: str  # absolute, native separators
    role: str  # "delete" | "write"


@dataclass
class Plan:
    targets: list[Target] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)

    @property
    def noop(self) -> bool:
        return not self.targets and not self.uncovered

    def add(self, path: str, role: str) -> None:
        if not any(t.path == path and t.role == role for t in self.targets):
            self.targets.append(Target(path, role))


def to_native(path: str) -> str:
    """Git-for-Windows bash speaks ``/c/Users/x``; Python on Windows wants ``C:\\Users\\x``."""
    if os.name == "nt":
        m = _DRIVE.match(path)
        if m:
            rest = path[2:] or "/"
            return f"{m.group(1).upper()}:{rest}".replace("/", "\\")
    return path


def _brace_expand(word: str) -> list[str]:
    m = _BRACE.search(word)
    if not m:
        return [word]
    out: list[str] = []
    for part in m.group(1).split(","):
        out.extend(_brace_expand(word[: m.start()] + part + word[m.end():]))
    return out


def _resolve(word: str, cwd: str, plan: Plan) -> list[str]:
    """Expand ``~``, braces, env vars and globs into absolute paths."""
    if "$(" in word or "`" in word:
        plan.uncovered.append(f"command substitution in {word!r}")
        return []
    results: list[str] = []
    for item in _brace_expand(word):
        if item.startswith(_IGNORED_PREFIXES):
            continue
        if "$" in item:
            expanded = os.path.expandvars(item)
            if "$" in expanded:
                plan.uncovered.append(f"unresolved variable in {word!r}")
                continue
            item = expanded
        item = os.path.expanduser(item)
        item = to_native(item)
        if not os.path.isabs(item):
            item = os.path.join(cwd, item)
        if any(ch in item for ch in "*?["):
            results.extend(sorted(glob.glob(item)))
        else:
            results.append(item)
    cleaned = []
    for p in results:
        p = os.path.normpath(p)
        if p.replace("\\", "/").startswith(_IGNORED_PREFIXES):
            continue
        cleaned.append(p)
    return cleaned


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _tokenize(command: str) -> list[str] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _positional(args: list[str]) -> list[str]:
    """Arguments that are not flags (everything after ``--`` counts as positional)."""
    out: list[str] = []
    after_dd = False
    for a in args:
        if after_dd:
            out.append(a)
        elif a == "--":
            after_dd = True
        elif a.startswith("-") and a != "-":
            continue
        else:
            out.append(a)
    return out


def _flag_value(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            return args[i + 1]
        for n in names:
            if n.startswith("--") and a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def _strip_prefix(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ENV_ASSIGN.match(tok) or tok in _WRAPPERS or tok == "env":
            i += 1
            continue
        break
    return seg[i:]


def _handle_redirects(seg: list[str], cwd: str, plan: Plan) -> list[str]:
    """Record ``>``/``>>`` targets and return the segment without them."""
    rest: list[str] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok in (">", ">>", ">|", "&>", "&>>") and i + 1 < len(seg):
            for p in _resolve(seg[i + 1], cwd, plan):
                plan.add(p, "write")
            i += 2
            continue
        if tok in (">&", "<", "<<", "<<<", "<&"):
            i += 2
            continue
        rest.append(tok)
        i += 1
    return rest


def _dest_paths(args: list[str], cwd: str, plan: Plan) -> tuple[list[str], list[str]]:
    """Return (sources, destinations) for mv/cp, honouring ``-t DIR``."""
    target_dir = _flag_value(args, "-t", "--target-directory")
    pos = _positional(args)
    if target_dir is not None:
        pos = [p for p in pos if p != target_dir]
        sources = [s for w in pos for s in _resolve(w, cwd, plan)]
        dirs = _resolve(target_dir, cwd, plan)
        return sources, [os.path.join(d, os.path.basename(s)) for d in dirs for s in sources]
    if len(pos) < 2:
        return [], []
    sources = [s for w in pos[:-1] for s in _resolve(w, cwd, plan)]
    dests = _resolve(pos[-1], cwd, plan)
    if not dests:
        return sources, []
    dest = dests[0]
    if os.path.isdir(dest) or len(sources) > 1:
        return sources, [os.path.join(dest, os.path.basename(s)) for s in sources]
    return sources, [dest]


def _segment(seg: list[str], cwd: str, plan: Plan, depth: int) -> None:
    seg = _handle_redirects(seg, cwd, plan)
    seg = _strip_prefix(seg)
    if not seg:
        return
    verb = os.path.basename(seg[0]).lower()
    args = seg[1:]

    if verb in _SHELLS:
        if "-c" in args or any(a.startswith("-") and "c" in a[1:] and not a.startswith("--") for a in args):
            inner = [a for a in args if not a.startswith("-")]
            if inner and depth < MAX_DEPTH:
                _parse_into(inner[0], cwd, plan, depth + 1)
            elif inner:
                plan.uncovered.append("nested shell too deep")
        return
    if verb == "eval":
        plan.uncovered.append("eval")
        return
    if verb in _FOREIGN_SHELLS or verb in _WINDOWS_ONLY:
        plan.uncovered.append(f"{verb} command")
        return
    if verb == "xargs":
        inner_verb = os.path.basename(next((a for a in args if not a.startswith("-")), "")).lower()
        if inner_verb in {"rm", "mv", "cp", "rmdir", "unlink", "sed", "tee", "shred"}:
            plan.uncovered.append(f"xargs {inner_verb}")
        return
    if verb in _INLINE_INTERPRETERS:
        if any(a in ("-c", "-e", "-p") for a in args) and any(_INLINE_DANGER.search(a) for a in args):
            plan.uncovered.append(f"inline {verb} script")
        return

    if verb in ("rm", "rmdir", "unlink", "shred"):
        for w in _positional(args):
            for p in _resolve(w, cwd, plan):
                if os.path.lexists(p):
                    plan.add(p, "delete")
    elif verb == "mv":
        sources, dests = _dest_paths(args, cwd, plan)
        for s in sources:
            if os.path.lexists(s):
                plan.add(s, "delete")
        for d in dests:
            plan.add(d, "write")
    elif verb == "cp" or verb == "install":
        _, dests = _dest_paths(args, cwd, plan)
        for d in dests:
            plan.add(d, "write")
    elif verb == "tee":
        for w in _positional(args):
            for p in _resolve(w, cwd, plan):
                plan.add(p, "write")
    elif verb == "sed":
        in_place = any(a == "--in-place" or a.startswith("--in-place=")
                       or (a.startswith("-i") and not a.startswith("--")) for a in args)
        if in_place:
            has_script_flag = any(a in ("-e", "-f") or a.startswith("--expression") for a in args)
            kept: list[str] = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                elif a in ("-e", "-f"):
                    skip_next = True
                elif not a.startswith("-i"):
                    kept.append(a)
            pos = _positional(kept)
            files = pos if has_script_flag else pos[1:]
            for w in files:
                for p in _resolve(w, cwd, plan):
                    plan.add(p, "write")
    elif verb == "truncate":
        for w in _positional(args):
            for p in _resolve(w, cwd, plan):
                plan.add(p, "write")
    elif verb == "dd":
        for a in args:
            if a.startswith("of="):
                for p in _resolve(a[3:], cwd, plan):
                    plan.add(p, "write")
    elif verb == "find":
        roots = []
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            roots.append(a)
        if "-delete" in args:
            for w in roots or ["."]:
                for p in _resolve(w, cwd, plan):
                    plan.add(p, "delete")
        elif "-exec" in args or "-execdir" in args:
            plan.uncovered.append("find -exec")
    elif verb == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        rest = [a for a in args if a != sub]
        if sub == "clean" or (sub == "reset" and "--hard" in rest) or sub in ("restore", "stash"):
            plan.uncovered.append(f"git {sub} rewrites the work tree")
        elif sub == "checkout" and ("--" in rest or "." in rest):
            plan.uncovered.append("git checkout rewrites the work tree")


def _parse_into(command: str, cwd: str, plan: Plan, depth: int) -> None:
    if "<<" in command and "<<<" not in command:
        plan.uncovered.append("heredoc")
        return
    for line in command.splitlines() or [command]:
        if not line.strip():
            continue
        tokens = _tokenize(line)
        if tokens is None:
            plan.uncovered.append("unparsable command")
            continue
        for seg in _split_segments(tokens):
            _segment(seg, cwd, plan, depth)


def parse_command(command: str, cwd: str) -> Plan:
    """Parse ``command`` (bash syntax) as it would run in ``cwd``."""
    plan = Plan()
    _parse_into(command, to_native(cwd), plan, 0)
    plan.uncovered = list(dict.fromkeys(plan.uncovered))
    return plan
