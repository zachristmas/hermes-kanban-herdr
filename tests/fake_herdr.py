"""A small stand-in for Herdr's local socket, used by the tests and the browser e2e.

Speaks the same JSON-lines protocol for the handful of methods the plugin uses
(pane.list / pane.get / pane.read / pane.report_metadata / events.subscribe), plus the
close/reopen set the plugin reaches only through the ``herdr`` CLI (pane.close / tab.create /
agent.start / agent.get -- modelled here too since ``stub_herdr_cli.py`` forwards those over this
same socket), and emits ``pane_updated`` events. Extra control methods (``fake.add``,
``fake.append``, ``fake.close``, ``fake.status``, ``fake.reset``) let a test drive it over the
same socket.

    python3 fake_herdr.py /tmp/fake-herdr.sock     # runs until killed
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading


class FakeHerdr:
    def __init__(self, path: str, subscribe_works: bool = True) -> None:
        self.path = path
        self.subscribe_works = subscribe_works
        self.panes: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self._pane_seq = 0
        self.subs: list[asyncio.StreamWriter] = []
        self.server = None

    # -- state -----------------------------------------------------------
    def add(self, pane_id: str, title: str = "", agent: str | None = None, status: str = "idle",
            cwd: str = "/tmp", tokens: dict | None = None, text: str = "", terminal_id: str | None = None,
            agent_session: dict | None = None) -> None:
        self.panes[pane_id] = {
            "pane_id": pane_id, "terminal_id": terminal_id or f"term_{pane_id.replace(':', '_')}",
            "workspace_id": pane_id.split(":")[0], "tab_id": pane_id.split(":")[0] + ":t1",
            "terminal_title": title, "terminal_title_stripped": title, "cwd": cwd, "foreground_cwd": cwd,
            "agent_status": status, "focused": False, "revision": 1, "tokens": dict(tokens or {}),
            "scroll": {"max_offset_from_bottom": 0, "offset_from_bottom": 0, "viewport_rows": 50},
            "_text": text, **({"agent": agent} if agent else {}),
            **({"agent_session": agent_session} if agent_session else {}),
        }

    def info(self, pane_id: str) -> dict:
        p = {k: v for k, v in self.panes[pane_id].items() if not k.startswith("_")}
        if not p["tokens"]:
            p.pop("tokens")
        return p

    def emit(self, pane_id: str) -> None:
        if not self.subscribe_works:
            return
        line = (json.dumps({"event": "pane_updated", "data": {"pane": self.info(pane_id), "type": "pane_updated"}}) + "\n").encode()
        for w in list(self.subs):
            try:
                w.write(line)
            except Exception:
                self.subs.remove(w)

    # -- protocol --------------------------------------------------------
    def _err(self, rid, code, message):
        return {"id": rid, "error": {"code": code, "message": message}}

    def dispatch(self, rid, method, params):
        pid = params.get("pane_id")
        if method == "ping":
            return {"id": rid, "result": {"type": "pong"}}
        if method == "pane.list":
            return {"id": rid, "result": {"type": "pane_list", "panes": [self.info(p) for p in self.panes]}}
        if method in ("pane.get", "pane.read", "pane.report_metadata") and pid not in self.panes:
            return self._err(rid, "pane_not_found", f"pane {pid} not found")
        if method == "pane.get":
            return {"id": rid, "result": {"type": "pane_info", "pane": self.info(pid)}}
        if method == "pane.read":
            lines = (self.panes[pid]["_text"]).split("\n")
            n = params.get("lines") or len(lines)
            text = "\n".join(lines[-n:])
            return {"id": rid, "result": {"type": "pane_read", "read": {
                "pane_id": pid, "workspace_id": self.panes[pid]["workspace_id"], "source": params.get("source"),
                "format": "text", "text": text, "revision": self.panes[pid]["revision"], "truncated": False}}}
        if method == "pane.close":
            self.panes.pop(pid, None)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "agent.get":
            name = params.get("name")
            agent = self.agents.get(name)
            if agent is None:
                return self._err(rid, "agent_not_found", f"no agent named {name!r}")
            return {"id": rid, "result": {"type": "agent_info", "agent": agent}}
        if method == "tab.create":
            self._pane_seq += 1
            ws = params.get("workspace") or "w1"
            new_pane_id = f"{ws}:auto{self._pane_seq}"
            self.add(new_pane_id, title=params.get("label") or "", cwd=params.get("cwd") or "/tmp")
            return {"id": rid, "result": {"type": "tab_created",
                     "root_pane": {"pane_id": new_pane_id, "workspace_id": ws}}}
        if method == "agent.start":
            name, kind, target = params.get("name"), params.get("kind"), params.get("pane_id")
            if target not in self.panes:
                return self._err(rid, "pane_not_found", f"pane {target} not found")
            session_id = params.get("resume_session_id") or f"sess-{name}"
            session = {"agent": kind, "kind": "id", "source": f"herdr:{kind}", "value": session_id}
            self.panes[target]["agent"] = kind
            self.panes[target]["agent_session"] = session
            agent = {"name": name, "agent": kind, "pane_id": target,
                      "workspace_id": self.panes[target]["workspace_id"], "agent_session": session}
            self.agents[name] = agent
            return {"id": rid, "result": {"type": "agent_started", "agent": agent}}
        if method == "pane.report_metadata":
            toks = self.panes[pid]["tokens"]
            for k, v in (params.get("tokens") or {}).items():
                if v is None:
                    toks.pop(k, None)
                else:
                    toks[k] = v
            self.panes[pid]["revision"] += 1
            self.emit(pid)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "fake.add":
            self.add(**params)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "fake.append":
            self.panes[pid]["_text"] += params["text"]
            self.panes[pid]["revision"] += 1
            self.emit(pid)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "fake.status":
            self.panes[pid]["agent_status"] = params["status"]
            self.panes[pid]["revision"] += 1
            self.emit(pid)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "fake.close":
            self.panes.pop(pid, None)
            return {"id": rid, "result": {"type": "ok"}}
        if method == "fake.reset":
            self.panes.clear()
            return {"id": rid, "result": {"type": "ok"}}
        return self._err(rid, "unknown_method", method)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
                if method == "events.subscribe":
                    if not self.subscribe_works:
                        writer.write((json.dumps(self._err(rid, "unsupported", "subscriptions disabled")) + "\n").encode())
                        await writer.drain()
                        break
                    writer.write((json.dumps({"id": rid, "result": {"type": "subscription_started"}}) + "\n").encode())
                    await writer.drain()
                    self.subs.append(writer)
                    continue  # keep reading so a disconnect is noticed
                writer.write((json.dumps(self.dispatch(rid, method, params)) + "\n").encode())
                await writer.drain()
        finally:
            if writer in self.subs:
                self.subs.remove(writer)
            writer.close()

    async def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for w in list(self.subs):
            w.close()
        if os.path.exists(self.path):
            os.unlink(self.path)


class FakeHerdrThread:
    """Runs a FakeHerdr on its own event loop so a synchronous test client can use it."""

    def __init__(self, path: str, **kw) -> None:
        self.fake = FakeHerdr(path, **kw)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> "FakeHerdrThread":
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.fake.start(), self.loop).result(5)
        return self

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.fake.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def call(self, fn, *a, **kw):
        done = threading.Event()
        out = {}

        def run():
            out["v"] = fn(*a, **kw)
            done.set()

        self.loop.call_soon_threadsafe(run)
        done.wait(5)
        return out.get("v")

    def add(self, *a, **kw): return self.call(self.fake.add, *a, **kw)
    def append(self, pane_id, text): return self.call(lambda: self.fake.dispatch("x", "fake.append", {"pane_id": pane_id, "text": text}))
    def close_pane(self, pane_id): return self.call(lambda: self.fake.dispatch("x", "fake.close", {"pane_id": pane_id}))
    def set_status(self, pane_id, status): return self.call(lambda: self.fake.dispatch("x", "fake.status", {"pane_id": pane_id, "status": status}))
    def tokens(self, pane_id): return self.call(lambda: dict(self.fake.panes[pane_id]["tokens"]))


if __name__ == "__main__":
    sock = sys.argv[1]
    fake = FakeHerdr(sock)

    async def main() -> None:
        await fake.start()
        print("ready", sock, flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
