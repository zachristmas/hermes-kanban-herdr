"""The ``kanban_herdr_link`` agent tool: tag a Herdr pane with the kanban card it works.

The kanban dashboard's "Herdr panes" section finds a card's panes by the pane tokens ``kanban_task`` /
``kanban_board``. This tool sets (or clears) them through the ``herdr`` CLI and reads them back, so a
dispatching agent doesn't compose the command itself or guess the flags. The tokens are display hints
for the dashboard, never authorization. Nothing here sends input to a pane or closes one.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from typing import Any

logger = logging.getLogger("hermes.plugins.kanban_herdr")

TOOL_NAME = "kanban_herdr_link"
TOOLSET = "kanban_herdr"
SOURCE = "kanban-link"
TOKEN_TASK = "kanban_task"
TOKEN_BOARD = "kanban_board"
TIMEOUT = 20

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MACHINE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LABEL_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")  # a machine label may contain spaces

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Tag a Herdr pane with the kanban card it is working, so the card's drawer in the Hermes dashboard "
        "shows the pane and a live view of it. Use it right after you create or resume a pane for a card "
        "(before starting the agent in it), and on an existing pane that is already working a card. "
        "action \"link\" sets the tag and reads it back; \"unlink\" removes it (only if it points at this "
        "card); \"show\" reads the pane's current tag. Take pane_id from Herdr's JSON output, never guess it. "
        "For a pane on another saved machine pass machine (its label or id from `herdr machine list`). "
        "The tag is a hint for the dashboard, not authorization: it does not change who owns the pane."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["link", "unlink", "show"],
                       "description": "link: tag the pane. unlink: remove this card's tag. show: read the tag."},
            "pane_id": {"type": "string", "description": "The Herdr pane id, e.g. w1:p2, from Herdr's output."},
            "task_id": {"type": "string", "description": "The kanban task id, e.g. t_1a2b3c4d. Required for link and unlink."},
            "board": {"type": "string", "description": "The kanban board slug (connect, gsd, titan, p5, cs, ...). "
                                                          "Give it whenever you know it."},
            "machine": {"type": "string", "description": "Only for a pane on another saved Herdr machine: its label or id."},
        },
        "required": ["action", "pane_id"],
    },
}


class ToolError(Exception):
    def __init__(self, code: str, note: str):
        super().__init__(note)
        self.code = code
        self.note = note


def herdr_command() -> list[str]:
    env = os.environ.get("HERDR_BIN")
    if env:
        return shlex.split(env, posix=(os.name != "nt"))
    found = shutil.which("herdr")
    if not found:
        pattern = os.path.join(os.path.expanduser("~"), ".herdr", "packages", "standalone", "releases", "*", "herdr*")
        hits = sorted(h for h in glob.glob(pattern) if os.path.basename(h).lower() in ("herdr", "herdr.exe"))
        found = hits[-1] if hits else None
    if not found:
        raise ToolError("herdr_not_found", "The herdr command isn't installed or isn't on PATH (set HERDR_BIN).")
    return [found]


def available() -> bool:
    try:
        herdr_command()
        return True
    except ToolError:
        return False


def _run(args: list[str], machine: str | None = None) -> dict:
    cmd = herdr_command() + (["--machine", machine] if machine else []) + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise ToolError("timeout", "Herdr did not answer in time.") from exc
    except OSError as exc:
        raise ToolError("herdr_not_found", f"Could not run herdr: {exc}") from exc
    # The CLI prints results as a JSON envelope on stdout, errors on stderr, and nothing at all when a
    # metadata update succeeds.
    msg = None
    for stream in (proc.stdout, proc.stderr):
        lines = (stream or "").strip().splitlines()
        try:
            parsed = json.loads(lines[-1]) if lines else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            msg = parsed
            break
    if msg is None:
        if proc.returncode == 0 and "report-metadata" in args and not (proc.stdout or "").strip():
            return {}
        raise ToolError("no_answer", "Herdr returned no JSON (is the Herdr server running?).")
    if isinstance(msg.get("error"), dict):
        err = msg["error"]
        raise ToolError(str(err.get("code", "error")), str(err.get("message", "")))
    return msg.get("result") or {}


def _resolve_machine(machine: str) -> str:
    """Accept a saved machine's id or its label."""
    try:
        proc = subprocess.run(herdr_command() + ["machine", "list"], capture_output=True, text=True,
                              timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("no_answer", "Could not list saved Herdr machines.") from exc
    rows = [line.split("\t") for line in (proc.stdout or "").splitlines()]
    for cols in rows:
        if len(cols) >= 2 and machine in (cols[0], cols[1]) and _MACHINE_RE.match(cols[0]):
            return cols[0]
    raise ToolError("unknown_machine", f"No saved Herdr machine matches {machine!r}.")


def _tokens(pane: dict) -> dict:
    t = pane.get("tokens") or {}
    return {k: t[k] for k in (TOKEN_TASK, TOKEN_BOARD) if k in t}


def _check(value: Any, what: str, pattern: re.Pattern = _ID_RE, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise ToolError("invalid_argument", f"{what} is required.")
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise ToolError("invalid_argument", f"{what} looks wrong: {value!r}.")
    return value


def _do(args: dict) -> dict:
    action = args.get("action")
    if action not in ("link", "unlink", "show"):
        raise ToolError("invalid_argument", "action must be link, unlink or show.")
    pane_id = _check(args.get("pane_id"), "pane_id")
    task = _check(args.get("task_id"), "task_id", required=action != "show")
    board = _check(args.get("board"), "board", required=False)
    machine = _check(args.get("machine"), "machine", _LABEL_RE, required=False)
    mid = _resolve_machine(machine) if machine else None

    try:
        before = _run(["pane", "get", pane_id], mid).get("pane") or {}
    except ToolError as exc:
        if exc.code == "pane_not_found":
            raise ToolError("pane_not_found", f"Herdr has no pane {pane_id}. Take the id from Herdr's JSON output.") from exc
        raise
    if action == "show":
        return {"pane_id": pane_id, "tokens": _tokens(before)}

    if action == "link":
        cmd = ["pane", "report-metadata", pane_id, "--source", SOURCE, "--token", f"{TOKEN_TASK}={task}"]
        if board:
            cmd += ["--token", f"{TOKEN_BOARD}={board}"]
        else:
            cmd += ["--clear-token", TOKEN_BOARD]  # don't leave a stale board from an earlier card
        _run(cmd, mid)
        after = _tokens(_run(["pane", "get", pane_id], mid).get("pane") or {})
        verified = after.get(TOKEN_TASK) == task and (not board or after.get(TOKEN_BOARD) == board)
        if not verified:
            raise ToolError("not_verified", f"Herdr accepted the tag but reading it back shows {after}. Check `herdr pane get {pane_id}`.")
        return {"pane_id": pane_id, "tokens": after, "verified": True}

    # unlink: only our own tag
    current = _tokens(before)
    if current.get(TOKEN_TASK) != task:
        return {"pane_id": pane_id, "tokens": current, "changed": False,
                "note": "This pane isn't tagged with that task, so nothing was changed."}
    _run(["pane", "report-metadata", pane_id, "--source", SOURCE, "--clear-token", TOKEN_TASK, "--clear-token", TOKEN_BOARD], mid)
    after = _tokens(_run(["pane", "get", pane_id], mid).get("pane") or {})
    return {"pane_id": pane_id, "tokens": after, "changed": True, "verified": TOKEN_TASK not in after}


def handle(args: Any, **_: Any) -> str:
    try:
        return json.dumps({"ok": True, **_do(args if isinstance(args, dict) else {})})
    except ToolError as exc:
        return json.dumps({"ok": False, "error": exc.code, "note": exc.note})
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("kanban_herdr_link failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable", "note": "Could not reach Herdr; the pane was not tagged."})


def register(ctx: Any) -> None:
    if not hasattr(ctx, "register_tool"):
        return
    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handle, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🔗")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("kanban-herdr: could not register %s", TOOL_NAME, exc_info=True)
