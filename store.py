"""Content-addressed snapshot store and restore for undo-effects.

An *entry* records the state of some paths just before a tool call changed them
(``pre``) and just after (``post``).  Restoring an entry puts ``pre`` back, but only
when the path still looks like ``post`` (nobody changed it since) unless ``force``.
Every restore snapshots what it is about to overwrite first, so a restore can itself
be undone.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MB = 1024 * 1024
PROTECTED_NAMES = (
    ".env", ".env.*", "auth.json", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.key",
    "*.p12", "*.pfx", ".netrc", ".git-credentials", "credentials", "credentials.json",
    "*.kdbx", "secrets.*",
)
PROTECTED_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube", ".azure", ".gcloud"}
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git"}


class _OutOfBudget(Exception):
    def __init__(self, reason: str):
        self.reason = reason


@dataclass
class Limits:
    max_file: int = 50 * MB
    max_entry: int = 200 * MB
    max_total: int = 2048 * MB
    max_files_per_dir: int = 20000
    budget_s: float = 8.0


@dataclass
class _Run:
    limits: Limits
    deadline: float
    copy: bool
    bytes_copied: int = 0
    files: int = 0
    skipped: list[str] = field(default_factory=list)


def _is_protected(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in PROTECTED_NAMES)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, root: str | os.PathLike[str], limits: Limits | None = None):
        self.root = Path(root)
        self.limits = limits or Limits()
        self.objects = self.root / "objects"
        self.entries_dir = self.root / "entries"

    # ------------------------------------------------------------------ scanning
    def _put_object(self, src: Path, run: _Run) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                if time.monotonic() > run.deadline:
                    raise _OutOfBudget("time_budget")
                h.update(chunk)
                size += len(chunk)
        sha = h.hexdigest()
        if run.copy:
            dest = self.objects / sha[:2] / sha
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
                os.close(fd)
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dest)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            run.bytes_copied += size
        return sha, size

    def _scan_file(self, path: Path, run: _Run) -> dict[str, Any]:
        st = path.lstat()
        if _is_protected(path.name):
            run.skipped.append(f"{path} (protected file name)")
            return {"kind": "skipped", "reason": "protected"}
        if st.st_size > run.limits.max_file:
            run.skipped.append(f"{path} (larger than {run.limits.max_file // MB} MB)")
            return {"kind": "skipped", "reason": "too_large", "size": st.st_size}
        if run.copy and run.bytes_copied + st.st_size > run.limits.max_entry:
            run.skipped.append(f"{path} (entry size cap {run.limits.max_entry // MB} MB)")
            return {"kind": "skipped", "reason": "entry_cap", "size": st.st_size}
        sha, size = self._put_object(path, run)
        run.files += 1
        return {"kind": "file", "sha": sha, "size": size, "mode": stat.S_IMODE(st.st_mode),
                "mtime": st.st_mtime}

    def _scan(self, path: Path, run: _Run) -> dict[str, Any]:
        """Describe ``path`` (and, for directories, everything under it)."""
        try:
            if time.monotonic() > run.deadline:
                raise _OutOfBudget("time_budget")
            if not os.path.lexists(path):
                return {"kind": "absent"}
            if path.is_symlink():
                return {"kind": "symlink", "target": os.readlink(path)}
            if path.is_file():
                return self._scan_file(path, run)
            if path.is_dir():
                if path.name in PROTECTED_DIRS:
                    run.skipped.append(f"{path} (protected directory)")
                    return {"kind": "skipped", "reason": "protected"}
                children: dict[str, Any] = {}
                for child in sorted(path.iterdir(), key=lambda p: p.name):
                    if child.name in SKIP_DIRS and child.is_dir() and not child.is_symlink():
                        run.skipped.append(f"{child} (dependency or VCS directory)")
                        children[child.name] = {"kind": "skipped", "reason": "skip_dir"}
                        continue
                    if run.files > run.limits.max_files_per_dir:
                        raise _OutOfBudget("too_many_files")
                    children[child.name] = self._scan(child, run)
                return {"kind": "dir", "mode": stat.S_IMODE(path.stat().st_mode), "children": children}
            return {"kind": "skipped", "reason": "special_file"}
        except _OutOfBudget:
            raise
        except OSError as exc:
            run.skipped.append(f"{path} ({exc.__class__.__name__})")
            return {"kind": "skipped", "reason": "unreadable"}

    def _new_run(self, copy: bool, budget_s: float | None = None) -> _Run:
        return _Run(self.limits, time.monotonic() + (budget_s or self.limits.budget_s), copy)

    @staticmethod
    def digest(node: dict[str, Any]) -> str:
        kind = node["kind"]
        if kind == "file":
            return "f:" + node["sha"]
        if kind == "symlink":
            return "l:" + node["target"]
        if kind == "dir":
            inner = json.dumps({k: Store.digest(v) for k, v in sorted(node["children"].items())})
            return "d:" + hashlib.sha256(inner.encode()).hexdigest()
        return kind + (":" + node.get("reason", "") if kind == "skipped" else "")

    # ------------------------------------------------------------------ entries
    def _entry_path(self, entry_id: str) -> Path:
        return self.entries_dir / f"{entry_id}.json"

    def _save(self, entry: dict[str, Any]) -> None:
        _atomic_write(self._entry_path(entry["id"]), json.dumps(entry, indent=1).encode())

    def load(self, entry_id: str) -> dict[str, Any]:
        return json.loads(self._entry_path(entry_id).read_text("utf-8"))

    def begin(self, *, tool: str, summary: str, session_id: str,
              targets: list[tuple[str, str]], uncovered: list[str]) -> dict[str, Any] | None:
        """Snapshot ``targets`` (``(path, role)`` pairs) before a tool call runs."""
        if not targets and not uncovered:
            return None
        run = self._new_run(copy=True)
        items: list[dict[str, Any]] = []
        notes = list(uncovered)
        for path, role in targets:
            try:
                pre = self._scan(Path(path), run)
            except _OutOfBudget as exc:
                pre = {"kind": "skipped", "reason": exc.reason}
                run.skipped.append(f"{path} ({exc.reason})")
            items.append({"path": path, "role": role, "pre": pre})
        notes.extend(run.skipped)
        entry = {
            "id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
            "ts": time.time(),
            "session_id": session_id,
            "tool": tool,
            "summary": summary[:300],
            "status": "pending",
            "items": items,
            "uncovered": notes,
            "restored": False,
        }
        self._save(entry)
        return entry

    def finish(self, entry: dict[str, Any], ok: bool, keep: bool = False) -> None:
        """Record the state after the tool call, so a restore can spot later edits."""
        run = self._new_run(copy=False, budget_s=4.0)
        for item in entry["items"]:
            try:
                item["post"] = self._scan(Path(item["path"]), run)
            except _OutOfBudget as exc:
                item["post"] = {"kind": "skipped", "reason": exc.reason}
        unchanged = all(self.digest(i["post"]) == self.digest(i["pre"]) for i in entry["items"])
        if unchanged and not entry["uncovered"] and not keep:
            # The call was refused, failed early, or changed nothing: keep the timeline free of noise.
            try:
                self._entry_path(entry["id"]).unlink()
            except OSError:
                pass
            return
        entry["status"] = "done" if ok else "failed"
        self._save(entry)

    def list(self, session_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.entries_dir.is_dir():
            return []
        entries = []
        for p in self.entries_dir.glob("*.json"):
            try:
                e = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if session_id and e.get("session_id") != session_id:
                continue
            entries.append(e)
        entries.sort(key=lambda e: e["ts"], reverse=True)
        return entries[:limit]

    # ------------------------------------------------------------------ restore
    def _materialize(self, path: Path, node: dict[str, Any]) -> None:
        kind = node["kind"]
        if kind == "file":
            src = self.objects / node["sha"][:2] / node["sha"]
            if not src.is_file():
                raise FileNotFoundError(f"snapshot object missing for {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".undo-")
            os.close(fd)
            try:
                shutil.copyfile(src, tmp)
                os.chmod(tmp, node.get("mode", 0o644))
                os.utime(tmp, (node.get("mtime", time.time()),) * 2)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        elif kind == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(node["target"], path)
        elif kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
            for name, child in node["children"].items():
                if child["kind"] != "skipped":
                    self._materialize(path / name, child)
            os.chmod(path, node.get("mode", 0o755))

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _reconcile(self, path: Path, pre: dict[str, Any], cur: dict[str, Any]) -> None:
        """Turn ``cur`` back into ``pre`` without ever deleting content the snapshot skipped."""
        if cur["kind"] == "skipped":
            return
        if pre["kind"] == "dir" and cur["kind"] == "dir":
            for name, child in cur["children"].items():
                if name not in pre["children"] and child["kind"] != "skipped":
                    self._remove(path / name)
            for name, child in pre["children"].items():
                if child["kind"] == "skipped":
                    continue
                now = cur["children"].get(name, {"kind": "absent"})
                if self.digest(now) != self.digest(child):
                    self._reconcile(path / name, child, now)
            os.chmod(path, pre.get("mode", 0o755))
            return
        if cur["kind"] != "absent":
            self._remove(path)
        if pre["kind"] != "absent":
            self._materialize(path, pre)

    def restore(self, entry_id: str, force: bool = False, dry_run: bool = False,
                session_id: str = "") -> dict[str, Any]:
        entry = self.load(entry_id)
        report: dict[str, Any] = {"id": entry_id, "restored": [], "unchanged": [], "conflicts": [],
                                  "unrestorable": [], "dry_run": dry_run}
        plans: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
        scan_run = self._new_run(copy=False, budget_s=30.0)
        for item in entry["items"]:
            path = Path(item["path"])
            pre, post = item["pre"], item.get("post")
            if pre["kind"] == "skipped":
                report["unrestorable"].append(f"{path} ({pre.get('reason')})")
                continue
            current = self._scan(path, scan_run)
            if self.digest(current) == self.digest(pre):
                report["unchanged"].append(str(path))
                continue
            if post is not None and self.digest(current) != self.digest(post) and not force:
                report["conflicts"].append(str(path))
                continue
            plans.append((item, path, current))
        if dry_run:
            report["restored"] = [str(p) for _, p, _ in plans]
            return report
        if plans:
            safety = self.begin(tool="undo-effects.restore", summary=f"before restoring {entry_id}",
                                session_id=session_id or entry.get("session_id", ""),
                                targets=[(str(p), "write") for _, p, _ in plans], uncovered=[])
            if safety:
                report["safety_entry"] = safety["id"]
        for item, path, current in plans:
            try:
                self._reconcile(path, item["pre"], current)
                report["restored"].append(str(path))
            except OSError as exc:
                report["conflicts"].append(f"{path} ({exc})")
        if plans and safety:
            self.finish(safety, True, keep=True)
        if report["restored"] and not report["conflicts"]:
            entry["restored"] = True
            self._save(entry)
        return report

    # ------------------------------------------------------------------ housekeeping
    def prune(self, max_age_days: float = 14.0, keep_last: int = 200) -> dict[str, int]:
        entries = self.list(limit=10**9)
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for i, e in enumerate(entries):
            if e["ts"] < cutoff or i >= keep_last:
                try:
                    self._entry_path(e["id"]).unlink()
                    removed += 1
                except OSError:
                    pass
        live: set[str] = set()

        def collect(node: dict[str, Any]) -> None:
            if node.get("kind") == "file":
                live.add(node["sha"])
            for child in node.get("children", {}).values():
                collect(child)

        for e in self.list(limit=10**9):
            for item in e["items"]:
                collect(item["pre"])
        freed = 0
        if self.objects.is_dir():
            for f in self.objects.rglob("*"):
                if f.is_file() and f.name not in live and not f.name.startswith(".tmp-"):
                    freed += f.stat().st_size
                    f.unlink()
        return {"entries_removed": removed, "bytes_freed": freed}

    def total_size(self) -> int:
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file()) if self.root.is_dir() else 0
