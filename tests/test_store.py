import os
import time

import pytest

from undo_effects.store import Limits, Store


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return Store(tmp_path / "store"), work


def snap(store, targets, uncovered=(), tool="terminal"):
    return store.begin(tool=tool, summary="test", session_id="s1",
                       targets=[(str(p), r) for p, r in targets], uncovered=list(uncovered))


def test_delete_then_restore_is_byte_identical(env):
    store, work = env
    blob = work / "blob.bin"
    blob.write_bytes(bytes(range(256)) * 1000)
    empty = work / "empty.txt"
    empty.write_bytes(b"")
    entry = snap(store, [(blob, "delete"), (empty, "delete")])
    blob.unlink()
    empty.unlink()
    store.finish(entry, True)
    report = store.restore(entry["id"])
    assert sorted(report["restored"]) == sorted([str(blob), str(empty)])
    assert blob.read_bytes() == bytes(range(256)) * 1000
    assert empty.read_bytes() == b""


def test_overwrite_restore_and_conflict(env):
    store, work = env
    f = work / "a.txt"
    f.write_text("original")
    entry = snap(store, [(f, "write")], tool="write_file")
    f.write_text("agent version")
    store.finish(entry, True)
    f.write_text("human edit afterwards")
    report = store.restore(entry["id"])
    assert report["conflicts"] == [str(f)] and f.read_text() == "human edit afterwards"
    report = store.restore(entry["id"], force=True)
    assert report["restored"] == [str(f)] and f.read_text() == "original"


def test_created_file_is_removed_by_restore(env):
    store, work = env
    f = work / "new.txt"
    entry = snap(store, [(f, "write")], tool="write_file")
    f.write_text("created by agent")
    store.finish(entry, True)
    store.restore(entry["id"])
    assert not f.exists()


def test_directory_restore_and_skipped_dirs_are_never_deleted(env):
    store, work = env
    proj = work / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "m.py").write_text("code")
    (proj / "node_modules" / "pkg").mkdir(parents=True)
    (proj / "node_modules" / "pkg" / "i.js").write_text("dep")
    entry = snap(store, [(proj, "delete")])
    (proj / "src" / "m.py").unlink()
    (proj / "src").rmdir()
    store.finish(entry, True)
    (proj / "stray.txt").write_text("")  # edits after the action make the dir a conflict...
    assert store.restore(entry["id"])["conflicts"]
    (proj / "stray.txt").unlink()
    report = store.restore(entry["id"])
    assert report["restored"] == [str(proj)]
    assert (proj / "src" / "m.py").read_text() == "code"
    assert (proj / "node_modules" / "pkg" / "i.js").read_text() == "dep"


def test_force_restore_keeps_skipped_content(env):
    store, work = env
    proj = work / "p"
    (proj / "node_modules").mkdir(parents=True)
    (proj / "node_modules" / "x.js").write_text("dep")
    (proj / "a.txt").write_text("a")
    entry = snap(store, [(proj, "delete")])
    (proj / "a.txt").unlink()
    store.finish(entry, True)
    (proj / "extra.txt").write_text("added later")
    store.restore(entry["id"], force=True)
    assert (proj / "a.txt").read_text() == "a"
    assert not (proj / "extra.txt").exists()
    assert (proj / "node_modules" / "x.js").read_text() == "dep"


def test_protected_and_oversized_files_are_reported_not_saved(tmp_path):
    store = Store(tmp_path / "store", Limits(max_file=10))
    work = tmp_path / "w"
    work.mkdir()
    secret = work / ".env"
    secret.write_text("TOKEN=1")
    big = work / "big.bin"
    big.write_bytes(b"x" * 100)
    entry = snap(store, [(secret, "delete"), (big, "delete")])
    assert len(entry["uncovered"]) == 2
    secret.unlink()
    big.unlink()
    store.finish(entry, True)
    report = store.restore(entry["id"])
    assert len(report["unrestorable"]) == 2 and not secret.exists() and not big.exists()
    assert not any(store.objects.rglob("*")) or all(not f.is_file() for f in store.objects.rglob("*"))


def test_restore_keeps_a_safety_copy_and_can_be_undone(env):
    store, work = env
    f = work / "a.txt"
    f.write_text("v1")
    entry = snap(store, [(f, "write")], tool="write_file")
    f.write_text("v2")
    store.finish(entry, True)
    report = store.restore(entry["id"])
    assert f.read_text() == "v1"
    store.restore(report["safety_entry"])
    assert f.read_text() == "v2"


def test_unchanged_calls_leave_no_entry(env):
    store, work = env
    f = work / "keep.txt"
    f.write_text("same")
    entry = snap(store, [(f, "delete")])
    store.finish(entry, True)  # tool refused or did nothing
    assert store.list() == []


def test_uncovered_only_entries_are_kept(env):
    store, _ = env
    entry = snap(store, [], uncovered=["git clean rewrites the work tree"])
    store.finish(entry, True)
    assert len(store.list()) == 1 and store.list()[0]["items"] == []


def test_time_budget_cuts_a_big_snapshot_without_raising(tmp_path):
    store = Store(tmp_path / "store", Limits(budget_s=0.0))
    f = tmp_path / "f.txt"
    f.write_text("x")
    entry = snap(store, [(f, "delete")])
    assert entry["items"][0]["pre"]["kind"] == "skipped"
    assert any("time_budget" in u for u in entry["uncovered"])


def test_prune_removes_old_entries_and_orphan_objects(env):
    store, work = env
    f = work / "a.txt"
    f.write_text("data")
    entry = snap(store, [(f, "delete")])
    f.unlink()
    store.finish(entry, True)
    path = store._entry_path(entry["id"])
    data = path.read_text().replace(f'"ts": {entry["ts"]}', f'"ts": {time.time() - 40 * 86400}')
    path.write_text(data)
    res = store.prune(max_age_days=14)
    assert res["entries_removed"] == 1 and res["bytes_freed"] > 0
    assert store.list() == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_roundtrip(env):
    store, work = env
    target = work / "t.txt"
    target.write_text("t")
    link = work / "l"
    link.symlink_to(target)
    entry = snap(store, [(link, "delete")])
    link.unlink()
    store.finish(entry, True)
    store.restore(entry["id"])
    assert link.is_symlink() and os.readlink(link) == str(target)


@pytest.mark.parametrize("folder", [".ssh", ".kube", ".docker", ".aws", ".gnupg"])
def test_file_named_directly_inside_a_protected_directory_is_not_copied(env, folder):
    store, work = env
    f = work / folder / "config"
    f.parent.mkdir()
    f.write_text("token: not-for-the-store")
    entry = snap(store, [(f, "delete")])
    assert entry["items"][0]["pre"] == {"kind": "skipped", "reason": "protected"}
    assert any("protected directory" in note for note in entry["uncovered"])
    assert not store.objects.exists() or not any(p.is_file() for p in store.objects.rglob("*"))


def test_nested_folder_of_a_protected_directory_is_not_copied(env):
    store, work = env
    nested = work / ".kube" / "cache"
    nested.mkdir(parents=True)
    (nested / "token").write_text("t")
    entry = snap(store, [(nested, "delete")])
    assert entry["items"][0]["pre"]["children"]["token"] == {"kind": "skipped", "reason": "protected"}


SECRET = b"token: SECRET-TOKEN-123\n"


def _link_to_dir(link, target, kind):
    """A directory link of the given kind, or skip when this machine cannot make one."""
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("junctions exist on Windows only")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here ({exc})")


def _store_files(store):
    return [p for p in store.root.rglob("*") if p.is_file() and p.parent != store.entries_dir]


@pytest.fixture(params=["symlink", "junction"])
def link_kind(request):
    return request.param


@pytest.mark.parametrize("folder", [".kube", ".ssh"])
def test_file_reached_through_a_link_to_a_protected_directory_is_not_copied(env, tmp_path, folder, link_kind):
    store, work = env
    real = tmp_path / "home" / folder
    real.mkdir(parents=True)
    (real / "config").write_bytes(SECRET)
    _link_to_dir(work / "alias", real, link_kind)
    entry = snap(store, [(work / "alias" / "config", "delete")])
    assert entry["items"][0]["pre"] == {"kind": "skipped", "reason": "protected"}
    assert any("protected directory" in note for note in entry["uncovered"])
    assert not any(SECRET in p.read_bytes() for p in _store_files(store))


def test_folder_reached_through_a_link_to_a_protected_directory_is_not_copied(env, tmp_path, link_kind):
    store, work = env
    real = tmp_path / "home" / ".kube"
    (real / "cache").mkdir(parents=True)
    (real / "cache" / "token").write_bytes(SECRET)
    _link_to_dir(work / "alias", real, link_kind)
    entry = snap(store, [(work / "alias" / "cache", "delete")])
    assert entry["items"][0]["pre"]["children"]["token"] == {"kind": "skipped", "reason": "protected"}
    assert not any(SECRET in p.read_bytes() for p in _store_files(store))


def test_walking_a_folder_that_holds_a_link_to_a_protected_directory_copies_nothing_of_it(env, tmp_path, link_kind):
    store, work = env
    real = tmp_path / "home" / ".kube"
    real.mkdir(parents=True)
    (real / "config").write_bytes(SECRET)
    _link_to_dir(work / "alias", real, link_kind)
    (work / "notes.txt").write_text("ordinary")
    entry = snap(store, [(work, "delete")])
    children = entry["items"][0]["pre"]["children"]
    assert children["notes.txt"]["kind"] == "file"
    assert children["alias"]["kind"] == "symlink" or children["alias"] == {"kind": "skipped", "reason": "protected"}
    assert not any(SECRET in p.read_bytes() for p in _store_files(store))


def test_a_link_to_a_link_to_a_protected_directory_is_followed_to_the_end(env, tmp_path):
    store, work = env
    real = tmp_path / "home" / ".aws"
    real.mkdir(parents=True)
    (real / "config").write_bytes(SECRET)
    _link_to_dir(tmp_path / "hop", real, "symlink")
    _link_to_dir(work / "alias", tmp_path / "hop", "symlink")
    entry = snap(store, [(work / "alias" / "config", "delete")])
    assert entry["items"][0]["pre"] == {"kind": "skipped", "reason": "protected"}
    assert not any(SECRET in p.read_bytes() for p in _store_files(store))


@pytest.mark.parametrize("folder", [".KUBE", ".Ssh"])
def test_protected_directory_names_match_whatever_the_case(env, folder):
    store, work = env
    f = work / folder / "config"
    f.parent.mkdir()
    f.write_bytes(SECRET)
    entry = snap(store, [(f, "delete")])
    assert entry["items"][0]["pre"] == {"kind": "skipped", "reason": "protected"}
    assert not any(SECRET in p.read_bytes() for p in _store_files(store))


def test_a_link_to_an_ordinary_directory_is_still_saved_through(env, tmp_path, link_kind):
    store, work = env
    real = tmp_path / "home" / "project"
    real.mkdir(parents=True)
    (real / "main.py").write_text("print(1)")
    _link_to_dir(work / "alias", real, link_kind)
    entry = snap(store, [(work / "alias" / "main.py", "delete")])
    assert entry["items"][0]["pre"]["kind"] == "file"


def test_file_with_a_look_alike_parent_is_still_copied(env):
    store, work = env
    f = work / "ssh-notes" / "config"
    f.parent.mkdir()
    f.write_text("plain")
    entry = snap(store, [(f, "delete")])
    assert entry["items"][0]["pre"]["kind"] == "file"
