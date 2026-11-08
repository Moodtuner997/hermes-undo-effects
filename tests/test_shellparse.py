import os
import time

import pytest

from undo_effects.shellparse import parse_command, to_native


@pytest.fixture
def work(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "inner.txt").write_text("i")
    return tmp_path


def paths(plan, role=None):
    return sorted(os.path.basename(t.path) for t in plan.targets if role in (None, t.role))


def run(cmd, work):
    return parse_command(cmd, work.as_posix())


def test_rm_existing_and_missing(work):
    plan = run("rm -rf a.txt dir nothing.txt", work)
    assert paths(plan, "delete") == ["a.txt", "dir"]
    assert not plan.uncovered


def test_rm_double_dash_and_flags(work):
    plan = run("rm -f -- -weird a.txt", work)
    assert paths(plan) == ["a.txt"]


def test_rm_glob(work):
    plan = run("rm *.txt", work)
    assert paths(plan, "delete") == ["a.txt", "b.txt"]


def test_brace_expansion(work):
    plan = run("rm {a,b}.txt", work)
    assert paths(plan, "delete") == ["a.txt", "b.txt"]


def test_mv_rename_to_new_name(work):
    plan = run("mv a.txt c.txt", work)
    assert paths(plan, "delete") == ["a.txt"]
    assert paths(plan, "write") == ["c.txt"]


def test_mv_onto_existing(work):
    plan = run("mv a.txt b.txt", work)
    assert paths(plan, "write") == ["b.txt"]


def test_mv_into_directory(work):
    plan = run("mv a.txt b.txt dir", work)
    assert paths(plan, "write") == ["a.txt", "b.txt"]
    assert all(os.path.dirname(t.path).endswith("dir") for t in plan.targets if t.role == "write")


def test_mv_target_directory_flag(work):
    plan = run("mv -t dir a.txt", work)
    assert paths(plan, "write") == ["a.txt"]


def test_cp_destination(work):
    plan = run("cp a.txt b.txt", work)
    assert paths(plan) == ["b.txt"]
    assert plan.targets[0].role == "write"


def test_redirects(work):
    assert paths(run("echo hi > a.txt", work), "write") == ["a.txt"]
    assert paths(run("echo hi >> b.txt", work), "write") == ["b.txt"]
    assert paths(run("make 2>/dev/null", work)) == []


def test_tee_and_sed_in_place(work):
    assert paths(run("echo x | tee a.txt", work), "write") == ["a.txt"]
    assert paths(run("sed -i 's/a/b/' a.txt", work), "write") == ["a.txt"]
    assert paths(run("sed -i.bak -e 's/a/b/' a.txt b.txt", work), "write") == ["a.txt", "b.txt"]
    assert paths(run("sed 's/a/b/' a.txt", work)) == []


def test_find_delete(work):
    plan = run("find dir -name '*.txt' -delete", work)
    assert paths(plan, "delete") == ["dir"]


def test_find_exec_is_uncovered(work):
    plan = run("find . -exec rm {} ;", work)
    assert plan.uncovered == ["find -exec"]


def test_chained_commands_and_wrappers(work):
    plan = run("cd dir && sudo rm inner.txt; env FOO=1 rm a.txt", work)
    assert "a.txt" in paths(plan, "delete")


def test_bash_c_recursion(work):
    plan = run("bash -c 'rm a.txt'", work)
    assert paths(plan, "delete") == ["a.txt"]


def test_git_worktree_rewrites_are_uncovered(work):
    for cmd in ("git clean -fd", "git reset --hard HEAD~1", "git checkout -- ."):
        assert run(cmd, work).uncovered, cmd
    assert run("git status", work).noop
    assert run("git commit -m x", work).noop


def test_uncovered_constructs(work):
    assert run("rm $(ls)", work).uncovered
    assert run("rm $UNDEFINED_VAR_XYZ/x", work).uncovered
    assert run("cat <<EOF > a.txt\nhi\nEOF", work).uncovered == ["heredoc"]
    assert run("powershell -Command Remove-Item a.txt", work).uncovered
    assert run("python -c \"import shutil; shutil.rmtree('dir')\"", work).uncovered
    assert run("echo 'unbalanced", work).uncovered == ["unparsable command"]
    assert run("ls | xargs rm", work).uncovered


def test_harmless_commands_are_noop(work):
    for cmd in ("ls -la", "cat a.txt", "grep -r x .", "python script.py", "npm test", "echo hi"):
        assert run(cmd, work).noop, cmd


def test_dev_null_and_proc_ignored(work):
    assert run("rm /dev/null", work).noop
    assert run("echo x > /dev/null", work).noop


@pytest.mark.skipif(os.name != "nt", reason="MSYS paths only on Windows")
def test_msys_paths_translate():
    assert to_native("/c/Users/x/f.txt") == "C:\\Users\\x\\f.txt"
    assert to_native("/c") == "C:\\"


def test_brace_groups_are_capped(work):
    started = time.perf_counter()
    plan = run("rm " + "{a,b}" * 24, work)  # 2**24 words if expanded in full
    assert time.perf_counter() - started < 1.0
    assert not plan.targets
    assert plan.uncovered and "brace expansion too large" in plan.uncovered[0]


def test_brace_expansion_up_to_the_cap_still_expands(work):
    assert paths(run("rm {a,b}.txt", work), "delete") == ["a.txt", "b.txt"]
    assert not run("rm " + "{a,b}" * 8, work).uncovered  # 256 words: the cap itself
    assert run("rm " + "{a,b}" * 9, work).uncovered  # 512 words


def test_install_and_cp_option_values_are_not_paths(work):
    for cmd in ("install -m 644 a.txt b.txt", "install -m755 a.txt b.txt", "install --mode=755 a.txt b.txt",
                "install -o root -g root -m 0644 a.txt b.txt", "cp -S .bak a.txt b.txt"):
        assert paths(run(cmd, work), "write") == ["b.txt"], cmd
