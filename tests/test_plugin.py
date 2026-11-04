import shutil

import pytest

import undo_effects as plugin


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {}
        self.middleware, self.commands, self.cli = {}, {}, {}

    def get_config(self):
        return self.config

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn):
        self.cli[name] = (setup_fn, handler_fn)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    c = FakeCtx()
    plugin.register(c)
    return c


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    return w


def call(ctx, tool, args, action):
    return ctx.middleware["tool_execution"](tool_name=tool, args=args, session_id="s1",
                                            next_call=lambda *_: action())


def test_registers_everything(ctx):
    assert set(ctx.middleware) == {"tool_execution"}
    assert "undo-effects" in ctx.commands and "undo-effects" in ctx.cli


def test_rm_rf_then_slash_restore(ctx, work):
    d = work / "proj"
    d.mkdir()
    (d / "a.txt").write_text("keep me")
    out = call(ctx, "terminal", {"command": "rm -rf proj", "workdir": str(work)},
               lambda: shutil.rmtree(d) or '{"exit_code": 0}')
    assert out == '{"exit_code": 0}'
    listing = ctx.commands["undo-effects"]("list", session_id="s1")
    assert "terminal" in listing and "rm -rf proj" in listing
    text = ctx.commands["undo-effects"]("restore 1", session_id="s1")
    assert "Restored 1 path(s)" in text
    assert (d / "a.txt").read_text() == "keep me"


def test_write_file_overwrite_and_last(ctx, work):
    f = work / "main.py"
    f.write_text("print('v1')")
    call(ctx, "write_file", {"path": str(f), "content": "print('v2')"}, lambda: f.write_text("print('v2')"))
    assert "Restored" in ctx.commands["undo-effects"]("last", session_id="s1")
    assert f.read_text() == "print('v1')"


def test_v4a_patch_covers_every_file(ctx, work):
    a, b = work / "a.txt", work / "b.txt"
    a.write_text("A")
    b.write_text("B")
    patch = f"*** Begin Patch\n*** Update File: {a}\n@@\n-A\n+A2\n*** Delete File: {b}\n*** End Patch"

    def apply():
        a.write_text("A2")
        b.unlink()

    call(ctx, "patch", {"mode": "patch", "patch": patch}, apply)
    ctx.commands["undo-effects"]("restore 1", session_id="s1")
    assert a.read_text() == "A" and b.read_text() == "B"


def test_refused_call_leaves_no_trace(ctx, work):
    f = work / "x.txt"
    f.write_text("x")
    call(ctx, "terminal", {"command": "rm x.txt", "workdir": str(work)}, lambda: '{"error": "denied"}')
    assert "No recorded effects" in ctx.commands["undo-effects"]("list", session_id="s1")


def test_downstream_error_is_reraised_and_recorded(ctx, work):
    f = work / "x.txt"
    f.write_text("x")

    def boom():
        f.unlink()
        raise RuntimeError("tool crashed")

    with pytest.raises(RuntimeError, match="tool crashed"):
        call(ctx, "terminal", {"command": "rm x.txt", "workdir": str(work)}, boom)
    ctx.commands["undo-effects"]("restore 1", session_id="s1")
    assert f.read_text() == "x"


def test_snapshot_failure_never_blocks_the_tool(ctx, work, monkeypatch):
    monkeypatch.setattr(plugin.Store, "begin", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    f = work / "x.txt"
    f.write_text("x")
    assert call(ctx, "terminal", {"command": "rm x.txt", "workdir": str(work)}, lambda: f.unlink() or "ok") == "ok"
    assert not f.exists()


def test_remote_terminal_backend_is_left_alone(ctx, work, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    f = work / "x.txt"
    f.write_text("x")
    call(ctx, "terminal", {"command": "rm x.txt", "workdir": str(work)}, lambda: f.unlink())
    assert "No recorded effects" in ctx.commands["undo-effects"]("list all")


def test_uncovered_commands_show_up_honestly(ctx, work):
    call(ctx, "terminal", {"command": "git clean -fd", "workdir": str(work)}, lambda: "ok")
    listing = ctx.commands["undo-effects"]("list", session_id="s1")
    assert "not covered" in listing and "git clean" in listing
    assert "nothing to restore" in ctx.commands["undo-effects"]("restore 1", session_id="s1")


def test_other_tools_and_harmless_commands_are_ignored(ctx, work):
    call(ctx, "read_file", {"path": "x"}, lambda: "ok")
    call(ctx, "terminal", {"command": "ls -la", "workdir": str(work)}, lambda: "ok")
    assert "No recorded effects" in ctx.commands["undo-effects"]("list all")


def test_dry_run_changes_nothing(ctx, work):
    f = work / "x.txt"
    f.write_text("x")
    call(ctx, "terminal", {"command": "rm x.txt", "workdir": str(work)}, lambda: f.unlink())
    assert "Would restore 1" in ctx.commands["undo-effects"]("restore 1 --dry-run", session_id="s1")
    assert not f.exists()


def test_doctor_and_help(ctx):
    assert "terminal backend: local" in ctx.commands["undo-effects"]("doctor")
    assert "restore N|last" in ctx.commands["undo-effects"]("help")


def test_restore_can_be_redone_by_id_and_safety_copies_stay_out_of_the_list(ctx, work):
    import re

    f = work / "a.txt"
    f.write_text("v1")
    call(ctx, "write_file", {"path": str(f), "content": "v2"}, lambda: f.write_text("v2"))
    out = ctx.commands["undo-effects"]("restore 1", session_id="s1")
    assert f.read_text() == "v1"
    safety_id = re.search(r"`restore (\S+)` puts it back", out).group(1)
    assert "undo-effects.restore" not in ctx.commands["undo-effects"]("list", session_id="s1")
    ctx.commands["undo-effects"](f"restore {safety_id}", session_id="s1")
    assert f.read_text() == "v2"
