"""End-to-end check against a real Hermes install, without any LLM.

Run with the Hermes interpreter and a throwaway HERMES_HOME whose config.yaml enables the plugin
and sets the approval mode to ``manual`` (Hermes' default, ``smart``, asks a model):

    plugins: {enabled: [undo-effects]}
    approvals: {mode: manual}

    HERMES_HOME=/tmp/hk/home HERMES_INTERACTIVE=1 <hermes python> tests/e2e_hermes.py

It sends real tool calls through ``model_tools.handle_function_call`` (hooks, middleware,
approvals, real terminal) and checks that undo-effects saved and restored the files. The
dangerous-command prompt is answered by a stand-in for the human, so no model and no terminal are needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from hermes_cli.config import load_config_readonly
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
from tools.terminal_tool import set_approval_callback

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def tool(name: str, args: dict) -> dict:
    raw = handle_function_call(name, args, task_id="e2e", session_id="e2e-session")
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def slash(raw: str) -> str:
    from hermes_cli.plugins import get_plugin_command_handler  # noqa: PLC0415

    handler = get_plugin_command_handler("undo-effects")
    return handler(raw, session_id="e2e-session")


mode = (load_config_readonly().get("approvals") or {}).get("mode")
if mode != "manual":
    sys.exit(f"approvals.mode is {mode!r}: set `approvals: {{mode: manual}}` in the throwaway config.yaml")

discover_plugins(force=True)
work =Path(tempfile.mkdtemp(prefix="undo-e2e-"))
(work / "proj" / "src").mkdir(parents=True)
(work / "proj" / "src" / "main.py").write_text("print('original')\n")
(work / "notes.txt").write_text("my notes\n")
(work / "old.txt").write_text("old\n")

out = tool("terminal", {"command": "rm -f notes.txt", "workdir": str(work)})
check("terminal rm really deleted the file", not (work / "notes.txt").exists(), json.dumps(out)[:200])

main_py = str(work / "proj" / "src" / "main.py")
for _ in range(3):
    # Hermes refuses to overwrite a file the agent has not read in full, and sometimes counts a read of a file
    # created a moment ago as partial: an agent just reads again, so does this script.
    tool("read_file", {"path": main_py})
    out = tool("write_file", {"path": main_py, "content": "print('agent rewrote this')\n"})
    if "error" not in out:
        break
check("write_file really overwrote", "agent rewrote" in (work / "proj" / "src" / "main.py").read_text(), json.dumps(out)[:200])

out = tool("terminal", {"command": "mv old.txt older.txt", "workdir": str(work)})
check("terminal mv really renamed", (work / "older.txt").exists() and not (work / "old.txt").exists())

listing = slash("list")
print(listing)
check("timeline lists three effects", listing.count("\n       ") >= 3, listing[:300])

for _ in range(3):  # `last` skips safety copies and already-restored effects: mv, write_file, rm
    text = slash("last")
    print(text)

check("deleted file is back", (work / "notes.txt").read_text() == "my notes\n")
check("overwritten file is back", (work / "proj" / "src" / "main.py").read_text() == "print('original')\n")
check("renamed file is back", (work / "old.txt").exists())

# A dangerous command goes through Hermes' own approval gate, which the plugin must not bypass.
asked: list[str] = []
answer = {"choice": "deny"}


def stand_in_for_the_human(command: str, description: str, **_: object) -> str:
    asked.append(command)
    return answer["choice"]


set_approval_callback(stand_in_for_the_human)

before = slash("list")
out = tool("terminal", {"command": "rm -rf proj", "workdir": str(work)})
check("the approval gate still asks before rm -rf", asked == ["rm -rf proj"], str(asked))
check("a refused rm -rf deletes nothing", (work / "proj" / "src" / "main.py").exists(), json.dumps(out)[:200])
check("a refused rm -rf leaves no entry", slash("list") == before)

answer["choice"] = "once"
out = tool("terminal", {"command": "rm -rf proj", "workdir": str(work)})
check("an approved rm -rf really deleted the directory", not (work / "proj").exists(), json.dumps(out)[:200])
print(slash("last"))
check("directory is back after rm -rf", (work / "proj" / "src" / "main.py").read_text() == "print('original')\n")

print("\nartifacts in", work)
sys.exit(1 if failures else 0)
