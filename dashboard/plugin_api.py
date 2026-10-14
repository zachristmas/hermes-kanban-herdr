"""Kanban Herdr plugin backend, mounted at /api/plugins/kanban-herdr/.

Read-only window onto Herdr (the terminal workspace manager) for the kanban task drawer:

* which panes belong to a task, found through pane tokens (``kanban_task`` / ``kanban_board``, set
  with ``herdr pane report-metadata``) or a manual link made from the drawer;
* a live view of one pane at a time over a WebSocket, driven by Herdr's ``pane_updated`` events.

It talks to Herdr's local socket directly (JSON lines, one request per connection). It never sends
input, keys or close requests to a pane. The only writes are the link tokens and a small JSON file
of manual links and recently seen panes under the Hermes home.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import glob
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter()

SOURCE = "kanban-link"
TOKEN_TASK = "kanban_task"
TOKEN_BOARD = "kanban_board"

_ID_RE = re.compile(r"^[A-Za-z0-9_.:~-]{1,100}$")  # "~" joins a saved machine id to a pane id
_MACHINE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

READ_LINES = 500          # lines of scrollback sent with each snapshot
MAX_TEXT = 256 * 1024     # characters kept per snapshot (the tail)
COALESCE_SECONDS = 0.12   # merge bursts of pane updates into one read
RESYNC_SECONDS = 10.0     # re-read even without an event, to repair any drift
POLL_SECONDS = 0.7        # fallback when the event subscription is unavailable
REMOTE_POLL_SECONDS = 1.5 # panes on another machine are read over SSH, so poll more gently
MACHINES_TTL = 60.0       # how long the saved-machine list is reused
REMOTE_LIST_TTL = 4.0     # how long a remote pane list is reused (drawers poll every 5 s)
REMOTE_TIMEOUT = 15.0
REMOTE_WAIT = 3.0         # longest a drawer request waits on any one saved machine
REMOTE_FAIL_TTL = 30.0    # how long a machine that failed is left alone
MAX_STREAMS = 4           # live views at once, across all viewers
KEEP_LINKS = 20
KEEP_SEEN = 12
RPC_TIMEOUT = 3.0
LINE_LIMIT = 8 * 1024 * 1024


class HerdrUnavailable(Exception):
    """Herdr's socket is missing or not answering."""


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --- Herdr socket client -----------------------------------------------------

def socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def _use_cli() -> bool:
    """Python on Windows has no unix sockets, so talk to Herdr through its CLI there."""
    forced = os.environ.get("KANBAN_HERDR_TRANSPORT")
    if forced:
        return forced == "cli"
    return not hasattr(asyncio, "open_unix_connection")


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
        raise HerdrUnavailable("cannot find the herdr command (set HERDR_BIN)")
    return [found]


def cli_args(method: str, params: dict) -> list[str]:
    """Map the few socket methods this plugin uses onto the equivalent ``herdr`` CLI call."""
    pid = params.get("pane_id") or ""
    if method == "pane.list":
        return ["pane", "list"]
    if method == "pane.get":
        return ["pane", "get", pid]
    if method == "pane.read":
        return ["pane", "read", pid, "--source", str(params.get("source") or "recent"),
                "--lines", str(int(params.get("lines") or READ_LINES)), "--format", "text"]
    if method == "pane.report_metadata":
        args = ["pane", "report-metadata", pid, "--source", str(params.get("source") or SOURCE)]
        for name, value in (params.get("tokens") or {}).items():
            args += ["--clear-token", name] if value is None else ["--token", f"{name}={value}"]
        return args
    raise HerdrUnavailable(f"{method} is not available without the herdr socket")


async def _run_cli(cmd: list, timeout: float, stderr: bool = True) -> tuple[int, bytes, bytes]:
    """Run a command in a worker thread. asyncio subprocesses raise NotImplementedError on the selector
    event loop the Windows dashboard runs on, so they can't be used here. On timeout the process is killed
    and its pipes are dropped without waiting: ``herdr --machine`` leaves helper processes holding them."""
    def go():
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if stderr else subprocess.DEVNULL)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe and pipe.close()
                except Exception:  # noqa: BLE001
                    pass
            raise asyncio.TimeoutError() from exc
        return proc.returncode, out or b"", err or b""
    return await asyncio.to_thread(go)


async def _rpc_cli(method: str, params: dict, machine: Optional[str] = None) -> dict:
    cmd = herdr_command() + (["--machine", machine] if machine else []) + cli_args(method, params)
    try:
        returncode, out, err = await _run_cli(cmd, REMOTE_TIMEOUT if machine else RPC_TIMEOUT * 3)
    except asyncio.TimeoutError as exc:
        raise HerdrUnavailable("herdr did not answer in time") from exc
    except OSError as exc:
        raise HerdrUnavailable(f"cannot run herdr: {exc}") from exc
    text = out.decode("utf-8", "replace")
    # The CLI prints results as a JSON envelope on stdout, errors on stderr, and nothing at all when a
    # metadata update succeeds.
    msg = None
    for stream in (text, err.decode("utf-8", "replace")):
        try:
            parsed = json.loads(stream.strip().splitlines()[-1]) if stream.strip() else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and str(parsed.get("id", "")).startswith("cli:") and ("result" in parsed or "error" in parsed):
            msg = parsed
            break
    if method == "pane.read" and (msg is None or "error" not in msg):
        # ``herdr pane read`` prints the terminal text itself, not a JSON envelope.
        return {"read": {"pane_id": params.get("pane_id"), "source": params.get("source"), "format": "text",
                         "text": text, "revision": None, "truncated": False}}
    if msg is None:
        if method == "pane.report_metadata" and returncode == 0 and not text.strip():
            return {}
        raise HerdrUnavailable("herdr returned no JSON (is it running?)")
    return _unwrap(msg)


async def _connect():
    if not hasattr(asyncio, "open_unix_connection"):
        raise HerdrUnavailable("unix sockets are not available on this platform")
    try:
        return await asyncio.wait_for(asyncio.open_unix_connection(socket_path(), limit=LINE_LIMIT), RPC_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        raise HerdrUnavailable(f"cannot reach herdr at {socket_path()}: {exc}") from exc


def _unwrap(msg: dict) -> dict:
    if isinstance(msg.get("error"), dict):
        err = msg["error"]
        raise HerdrError(str(err.get("code", "error")), str(err.get("message", "")))
    return msg.get("result") or {}


def split_ref(ref: str) -> tuple[Optional[str], str]:
    """A pane on another saved machine is ``<machine id>~<pane id>``; a local pane is just its id."""
    machine, sep, pane = (ref or "").partition("~")
    if not sep:
        return None, ref
    if not _MACHINE_RE.match(machine):
        raise HerdrError("invalid_ref", "bad pane reference")
    return machine, pane


async def rpc(method: str, params: Optional[dict] = None, machine: Optional[str] = None) -> dict:
    params = dict(params or {})
    if params.get("pane_id"):
        ref_machine, params["pane_id"] = split_ref(params["pane_id"])
        machine = ref_machine or machine
    if machine:
        return await _rpc_cli(method, params, machine)  # other machines are reached through the herdr CLI
    if _use_cli():
        return await _rpc_cli(method, params)
    reader, writer = await _connect()
    try:
        writer.write((json.dumps({"id": "kh", "method": method, "params": params or {}}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), RPC_TIMEOUT)
        if not line:
            raise HerdrUnavailable("herdr closed the connection")
        return _unwrap(json.loads(line))
    except asyncio.TimeoutError as exc:
        raise HerdrUnavailable("herdr did not answer in time") from exc
    finally:
        writer.close()


async def subscribe(types: list[str]) -> AsyncIterator[dict]:
    """Yield event envelopes (``{"event": ..., "data": ...}``) until the connection ends."""
    if _use_cli():
        raise HerdrUnavailable("event subscriptions need the herdr socket")
    reader, writer = await _connect()
    try:
        req = {"id": "kh-sub", "method": "events.subscribe",
               "params": {"subscriptions": [{"type": t} for t in types]}}
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        first = await asyncio.wait_for(reader.readline(), RPC_TIMEOUT)
        if not first:
            raise HerdrUnavailable("herdr closed the connection")
        _unwrap(json.loads(first))
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                yield json.loads(line)
            except ValueError:
                continue
    finally:
        writer.close()


def public_pane(p: dict) -> dict:
    tokens = p.get("tokens") or {}
    return {
        "pane_id": p.get("pane_id"),
        "terminal_id": p.get("terminal_id"),
        "workspace_id": p.get("workspace_id"),
        "tab_id": p.get("tab_id"),
        "title": p.get("terminal_title_stripped") or p.get("terminal_title") or "",
        "agent": p.get("agent"),
        "agent_status": p.get("agent_status") or "unknown",
        "cwd": p.get("cwd"),
        "focused": bool(p.get("focused")),
        "revision": p.get("revision"),
        "task": tokens.get(TOKEN_TASK),
        "board": tokens.get(TOKEN_BOARD),
        "machine": p.get("_machine"),
    }


_machines_cache: dict[str, Any] = {"at": 0.0, "items": []}
_remote_cache: dict[str, tuple[float, list]] = {}
unreachable: list[str] = []  # labels of saved machines that did not answer on the last listing


async def saved_machines() -> list[dict]:
    """Enabled SSH machines saved in Herdr (``herdr machine list``: id, label, host, session, state)."""
    if time.time() - _machines_cache["at"] < MACHINES_TTL:
        return _machines_cache["items"]
    items: list[dict] = []
    try:
        _, out, _ = await _run_cli(herdr_command() + ["machine", "list"], RPC_TIMEOUT * 3, stderr=False)
        for line in out.decode("utf-8", "replace").splitlines():
            cols = line.split("\t")
            if len(cols) >= 5 and cols[4].strip() == "enabled" and _MACHINE_RE.match(cols[0]):
                items.append({"id": cols[0], "label": cols[1], "host": cols[2]})
    except (OSError, asyncio.TimeoutError, HerdrUnavailable):
        items = []
    _machines_cache.update(at=time.time(), items=items)
    return items


_remote_tasks: dict[str, "asyncio.Future"] = {}


async def _fetch_remote(m: dict) -> list[dict]:
    try:
        panes = (await rpc("pane.list", machine=m["id"])).get("panes") or []
    except Exception:
        _remote_cache[m["id"]] = (time.time(), [], False)
        raise
    for p in panes:
        p["pane_id"] = f'{m["id"]}~{p.get("pane_id")}'
        p["_machine"] = m["label"]
    _remote_cache[m["id"]] = (time.time(), panes, True)
    return panes


async def _remote_panes(m: dict) -> list[dict]:
    """One machine's panes. A slow or dead machine must not hold up the drawer: the fetch runs once in the
    background, a failure is remembered for REMOTE_FAIL_TTL, and the caller waits at most REMOTE_WAIT."""
    cached = _remote_cache.get(m["id"])
    if cached:
        age = time.time() - cached[0]
        if cached[2] and age < REMOTE_LIST_TTL:
            return cached[1]
        if not cached[2] and age < REMOTE_FAIL_TTL:
            raise HerdrUnavailable("machine did not answer recently")
    task = _remote_tasks.get(m["id"])
    if task is None or task.done():
        task = _remote_tasks[m["id"]] = asyncio.ensure_future(_fetch_remote(m))
        task.add_done_callback(lambda f: f.cancelled() or f.exception())  # retrieved; errors are cached above
    # Only wait for a machine that has not failed before; one that did is skipped until its retry lands.
    done, _ = await asyncio.wait({task}, timeout=REMOTE_WAIT if (not cached or cached[2]) else 0)
    if task in done:
        return task.result()
    if cached and cached[2]:
        return cached[1]  # stale beats nothing while the refresh is still running
    # Count it as down for now so the next poll does not wait again; the fetch overwrites this if it succeeds.
    _remote_cache[m["id"]] = (time.time(), [], False)
    raise HerdrUnavailable("machine is slow to answer")


async def list_panes() -> list[dict]:
    """Panes on this machine plus every enabled saved machine. A machine that does not answer is skipped
    (and named in ``unreachable``) rather than failing the whole list; only a dead local Herdr with no
    reachable machine raises ``HerdrUnavailable``."""
    local: list[dict] = []
    local_error: Optional[HerdrUnavailable] = None
    try:
        local = (await rpc("pane.list")).get("panes") or []
    except HerdrUnavailable as exc:
        local_error = exc
    machines = await saved_machines()
    results = await asyncio.gather(*[_remote_panes(m) for m in machines], return_exceptions=True)
    remote: list[dict] = []
    bad: list[str] = []
    for m, r in zip(machines, results):
        if isinstance(r, list):
            remote += r
        else:
            bad.append(m["label"])
    unreachable[:] = bad
    if local_error and not remote:
        raise local_error
    return local + remote


# --- link store (manual links + recently seen panes) --------------------------

def _data_dir() -> Path:
    override = os.environ.get("KANBAN_HERDR_DATA")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "kanban-herdr"


class Store:
    """links.json: {"links": {key: [..]}, "seen": {key: [..]}}, key = "<board>:<task>"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return _data_dir() / "links.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data.setdefault("links", {})
                data.setdefault("seen", {})
                return data
        except (OSError, ValueError):
            pass
        return {"links": {}, "seen": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".links-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self) -> dict:
        with self._lock:
            return self._load()

    def update(self, fn) -> None:
        with self._lock:
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            fn(data)
            if json.dumps(data, sort_keys=True) != before:
                self._save(data)


store = Store()


def key_of(board: Optional[str], task: str) -> str:
    return f"{board or 'default'}:{task}"


# --- matching ---------------------------------------------------------------

def match_reasons(pane: dict, task: str, board: Optional[str], manual_terminals: set[str]) -> list[str]:
    tokens = pane.get("tokens") or {}
    reasons = []
    if tokens.get(TOKEN_TASK) == task and (not board or not tokens.get(TOKEN_BOARD) or tokens[TOKEN_BOARD] == board):
        reasons.append("token")
    if pane.get("terminal_id") in manual_terminals:
        reasons.append("manual")
    return reasons


def _check_id(value: str, what: str) -> str:
    if not value or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"invalid {what}")
    return value


def _check_board(board: Optional[str]) -> Optional[str]:
    return _check_id(board, "board") if board else None


@router.get("/panes")
async def panes_for_task(task: str = Query(...), board: Optional[str] = Query(None)):
    _check_id(task, "task")
    board = _check_board(board)
    try:
        panes = await list_panes()
    except HerdrUnavailable as exc:
        return {"available": False, "reason": str(exc), "panes": [], "closed": []}
    key = key_of(board, task)
    data = store.read()
    manual = {e.get("terminal_id") for e in data["links"].get(key, [])}
    now = int(time.time())
    matched = []
    for p in panes:
        why = match_reasons(p, task, board, manual)
        if why:
            matched.append({**public_pane(p), "linked_by": why})

    def remember(d: dict) -> None:
        seen = d["seen"].setdefault(key, [])
        by_term = {e.get("terminal_id"): e for e in seen}
        for m in matched:
            e = by_term.get(m["terminal_id"])
            fresh = {"terminal_id": m["terminal_id"], "pane_id": m["pane_id"], "title": m["title"],
                     "agent": m["agent"], "last_status": m["agent_status"], "last_seen": now}
            if e is None:
                seen.append(fresh)
            elif (e.get("last_status"), e.get("title"), e.get("pane_id")) != (fresh["last_status"], fresh["title"], fresh["pane_id"]) \
                    or now - int(e.get("last_seen") or 0) > 60:
                e.update(fresh)
        del seen[:-KEEP_SEEN]

    if matched:
        store.update(remember)
    live_terms = {p.get("terminal_id") for p in panes}
    closed = [e for e in store.read()["seen"].get(key, []) if e.get("terminal_id") not in live_terms]
    return {"available": True, "panes": matched, "closed": closed[-KEEP_SEEN:], "unreachable": list(unreachable)}


@router.get("/all-panes")
async def all_panes():
    try:
        panes = await list_panes()
    except HerdrUnavailable as exc:
        return {"available": False, "reason": str(exc), "panes": []}
    return {"available": True, "panes": [public_pane(p) for p in panes], "unreachable": list(unreachable)}


class LinkBody(BaseModel):
    task: str
    pane_id: str
    board: Optional[str] = None


async def _get_pane(pane_id: str) -> dict:
    try:
        return (await rpc("pane.get", {"pane_id": pane_id})).get("pane") or {}
    except HerdrError as exc:
        if exc.code == "pane_not_found":
            raise HTTPException(status_code=404, detail="pane not found") from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HerdrUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/link")
async def link_pane(body: LinkBody):
    task = _check_id(body.task, "task")
    board = _check_board(body.board)
    pane = await _get_pane(_check_id(body.pane_id, "pane_id"))
    tokens: dict[str, Optional[str]] = {TOKEN_TASK: task}
    if board:
        tokens[TOKEN_BOARD] = board
    try:
        await rpc("pane.report_metadata", {"pane_id": body.pane_id, "source": SOURCE, "tokens": tokens})
    except (HerdrError, HerdrUnavailable) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    key = key_of(board, task)

    def add(d: dict) -> None:
        links = d["links"].setdefault(key, [])
        links[:] = [e for e in links if e.get("terminal_id") != pane.get("terminal_id")]
        links.append({"terminal_id": pane.get("terminal_id"), "pane_id": body.pane_id,
                      "title": public_pane(pane)["title"], "linked_at": int(time.time())})
        del links[:-KEEP_LINKS]

    store.update(add)
    return {"ok": True}


@router.post("/unlink")
async def unlink_pane(body: LinkBody):
    task = _check_id(body.task, "task")
    board = _check_board(body.board)
    _check_id(body.pane_id, "pane_id")
    terminal = None
    try:
        pane = (await rpc("pane.get", {"pane_id": body.pane_id})).get("pane") or {}
        terminal = pane.get("terminal_id")
        # Only clear tokens that point at this task; another card's link is not ours to remove.
        if (pane.get("tokens") or {}).get(TOKEN_TASK) == task:
            await rpc("pane.report_metadata", {"pane_id": body.pane_id, "source": SOURCE,
                                               "tokens": {TOKEN_TASK: None, TOKEN_BOARD: None}})
    except HerdrError as exc:
        if exc.code != "pane_not_found":
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HerdrUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    key = key_of(board, task)

    def drop(d: dict) -> None:
        for bucket in ("links", "seen"):
            items = d[bucket].get(key, [])
            items[:] = [e for e in items if e.get("pane_id") != body.pane_id and (terminal is None or e.get("terminal_id") != terminal)]

    store.update(drop)
    return {"ok": True}


# --- live stream ------------------------------------------------------------

_active_streams = 0


def clean_text(text: str) -> str:
    text = _CTRL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text[-MAX_TEXT:] if len(text) > MAX_TEXT else text


def _ws_authorized(ws: WebSocket) -> bool:
    """The dashboard's own WebSocket gate (?token= / ?ticket=); open only when the dashboard isn't importable."""
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


async def _watch(pane_id: str, dirty: asyncio.Event, status: dict) -> None:
    """Set ``dirty`` whenever the pane changes: from Herdr's events, or by polling if they are unavailable."""
    if "~" in pane_id:  # another machine: no event stream, so tick and let the reader compare text
        while True:
            await asyncio.sleep(REMOTE_POLL_SECONDS)
            dirty.set()
    try:
        async for ev in subscribe(["pane.updated"]):
            pane = (ev.get("data") or {}).get("pane") or {}
            if pane.get("pane_id") == pane_id:
                status["agent_status"] = pane.get("agent_status") or status.get("agent_status")
                dirty.set()
        return  # herdr ended the stream; the resync timer keeps the view fresh and reconnects below
    except (HerdrError, HerdrUnavailable) as exc:
        log.info("kanban-herdr: event subscription unavailable (%s); polling", exc)
    last = None
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            pane = (await rpc("pane.get", {"pane_id": pane_id})).get("pane") or {}
        except (HerdrError, HerdrUnavailable):
            dirty.set()
            continue
        if pane.get("revision") != last:
            last = pane.get("revision")
            status["agent_status"] = pane.get("agent_status") or status.get("agent_status")
            dirty.set()


async def _pump(ws: WebSocket, pane_id: str) -> None:
    status: dict[str, Any] = {}
    try:
        pane = (await rpc("pane.get", {"pane_id": pane_id})).get("pane") or {}
    except HerdrError as exc:
        await ws.send_json({"type": "closed" if exc.code == "pane_not_found" else "error", "error": exc.message})
        return
    except HerdrUnavailable as exc:
        await ws.send_json({"type": "error", "error": str(exc)})
        return
    status["agent_status"] = pane.get("agent_status") or "unknown"
    sent = {"text": None, "status": None}

    async def push(force: bool = False) -> bool:
        try:
            read = (await rpc("pane.read", {"pane_id": pane_id, "source": "recent", "lines": READ_LINES,
                                            "format": "text", "strip_ansi": True})).get("read") or {}
        except HerdrError as exc:
            if exc.code == "pane_not_found":
                await ws.send_json({"type": "closed"})
                return False
            await ws.send_json({"type": "error", "error": exc.message})
            return True
        except HerdrUnavailable as exc:
            await ws.send_json({"type": "error", "error": str(exc)})
            return True
        text = clean_text(read.get("text") or "")
        if force or text != sent["text"] or status["agent_status"] != sent["status"]:
            sent["text"], sent["status"] = text, status["agent_status"]
            await ws.send_json({"type": "snapshot", "text": text, "revision": read.get("revision"),
                                "agent_status": status["agent_status"]})
        return True

    dirty = asyncio.Event()
    watcher = asyncio.create_task(_watch(pane_id, dirty, status))

    async def receiver() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return

    recv = asyncio.create_task(receiver())
    try:
        if not await push(force=True):
            return
        while True:
            dtask = asyncio.create_task(dirty.wait())
            done, _ = await asyncio.wait({dtask, recv}, timeout=RESYNC_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            dtask.cancel()
            if recv in done:
                return
            if dtask in done:
                await asyncio.sleep(COALESCE_SECONDS)  # let a burst of updates settle into one read
            dirty.clear()
            if not await push():
                return
            if watcher.done():  # herdr ended the event stream: reconnect, but not in a tight loop
                await asyncio.sleep(1.0)
                watcher = asyncio.create_task(_watch(pane_id, dirty, status))
    finally:
        for t in (watcher, recv):
            t.cancel()


@router.websocket("/stream")
async def stream(ws: WebSocket):
    global _active_streams
    pane_id = ws.query_params.get("pane", "")
    if not _ws_authorized(ws) or not _ID_RE.match(pane_id):
        await ws.close(code=1008)
        return
    await ws.accept()
    if _active_streams >= MAX_STREAMS:
        await ws.send_json({"type": "error", "error": "too many live views are open"})
        await ws.close(code=1013)
        return
    _active_streams += 1
    try:
        await _pump(ws, pane_id)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:  # never take the dashboard worker down
        log.warning("kanban-herdr stream error: %s", exc)
    finally:
        _active_streams -= 1
        try:
            await ws.close()
        except Exception:
            pass
