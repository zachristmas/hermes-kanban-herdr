import asyncio
import importlib.util
import os
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from fake_herdr import FakeHerdrThread  # noqa: E402

spec = importlib.util.spec_from_file_location("kh_plugin_api", HERE.parent / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
sys.modules["kh_plugin_api"] = api  # as Hermes does, so pydantic can resolve string annotations
spec.loader.exec_module(api)


class Base(unittest.TestCase):
    subscribe_works = True

    def setUp(self):
        signal.alarm(30)  # a hung websocket must fail the test, not the run
        self.tmp = tempfile.mkdtemp(prefix="khtest-")
        self.sock = os.path.join(self.tmp, "h.sock")
        os.environ["HERDR_SOCKET_PATH"] = self.sock
        os.environ["KANBAN_HERDR_DATA"] = os.path.join(self.tmp, "data")
        self.herdr = FakeHerdrThread(self.sock, subscribe_works=self.subscribe_works).start()
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/kanban-herdr")
        self.client = TestClient(app)
        api._ws_authorized = lambda ws: True
        api._active_streams = 0
        api.COALESCE_SECONDS = 0.02
        api.POLL_SECONDS = 0.05
        self.herdr.add("w1:p1", title="gsd-t_a1", agent="claude", status="working", tokens={"kanban_task": "t_a1", "kanban_board": "gsd"}, text="one\ntwo\n")
        self.herdr.add("w1:p2", title="other", status="idle", tokens={"kanban_task": "t_zz"})
        self.herdr.add("w1:p3", title="shell", status="unknown")

    def tearDown(self):
        signal.alarm(0)
        self.herdr.stop()

    def url(self, path):
        return "/api/plugins/kanban-herdr" + path


class PaneListTests(Base):
    def test_matches_by_token_only_for_this_task(self):
        r = self.client.get(self.url("/panes"), params={"task": "t_a1", "board": "gsd"}).json()
        self.assertTrue(r["available"])
        self.assertEqual([p["pane_id"] for p in r["panes"]], ["w1:p1"])
        self.assertEqual(r["panes"][0]["linked_by"], ["token"])
        self.assertEqual(r["panes"][0]["agent_status"], "working")

    def test_board_mismatch_is_not_a_match(self):
        r = self.client.get(self.url("/panes"), params={"task": "t_a1", "board": "p5"}).json()
        self.assertEqual(r["panes"], [])

    def test_missing_board_still_matches(self):
        r = self.client.get(self.url("/panes"), params={"task": "t_a1"}).json()
        self.assertEqual(len(r["panes"]), 1)

    def test_bad_ids_are_rejected(self):
        self.assertEqual(self.client.get(self.url("/panes"), params={"task": "../x"}).status_code, 400)
        self.assertEqual(self.client.get(self.url("/panes"), params={"task": "t_a1", "board": "a b"}).status_code, 400)

    def test_unavailable_when_herdr_is_not_running(self):
        os.environ["HERDR_SOCKET_PATH"] = os.path.join(self.tmp, "nope.sock")
        r = self.client.get(self.url("/panes"), params={"task": "t_a1"}).json()
        self.assertFalse(r["available"])
        self.assertEqual(r["panes"], [])

    def test_closed_pane_is_remembered(self):
        self.client.get(self.url("/panes"), params={"task": "t_a1", "board": "gsd"})
        self.herdr.close_pane("w1:p1")
        r = self.client.get(self.url("/panes"), params={"task": "t_a1", "board": "gsd"}).json()
        self.assertEqual(r["panes"], [])
        self.assertEqual([c["pane_id"] for c in r["closed"]], ["w1:p1"])
        self.assertEqual(r["closed"][0]["last_status"], "working")

    def test_all_panes_lists_everything(self):
        r = self.client.get(self.url("/all-panes")).json()
        self.assertEqual({p["pane_id"] for p in r["panes"]}, {"w1:p1", "w1:p2", "w1:p3"})
        self.assertNotIn("_text", str(r))


class LinkTests(Base):
    def test_link_sets_tokens_and_shows_up(self):
        r = self.client.post(self.url("/link"), json={"task": "t_new", "board": "cs", "pane_id": "w1:p3"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.herdr.tokens("w1:p3"), {"kanban_task": "t_new", "kanban_board": "cs"})
        panes = self.client.get(self.url("/panes"), params={"task": "t_new", "board": "cs"}).json()["panes"]
        self.assertEqual([p["pane_id"] for p in panes], ["w1:p3"])
        self.assertEqual(sorted(panes[0]["linked_by"]), ["manual", "token"])

    def test_manual_link_survives_token_loss(self):
        self.client.post(self.url("/link"), json={"task": "t_new", "pane_id": "w1:p3"})
        self.herdr.call(lambda: self.herdr.fake.panes["w1:p3"]["tokens"].clear())
        panes = self.client.get(self.url("/panes"), params={"task": "t_new"}).json()["panes"]
        self.assertEqual(panes[0]["linked_by"], ["manual"])

    def test_unlink_clears_tokens_and_store(self):
        self.client.post(self.url("/link"), json={"task": "t_new", "pane_id": "w1:p3"})
        self.assertEqual(self.client.post(self.url("/unlink"), json={"task": "t_new", "pane_id": "w1:p3"}).status_code, 200)
        self.assertEqual(self.herdr.tokens("w1:p3"), {})
        self.assertEqual(self.client.get(self.url("/panes"), params={"task": "t_new"}).json()["panes"], [])

    def test_unlink_leaves_another_cards_tokens_alone(self):
        self.client.post(self.url("/unlink"), json={"task": "t_other", "pane_id": "w1:p2"})
        self.assertEqual(self.herdr.tokens("w1:p2"), {"kanban_task": "t_zz"})

    def test_link_unknown_pane_is_404(self):
        r = self.client.post(self.url("/link"), json={"task": "t_new", "pane_id": "w9:p9"})
        self.assertEqual(r.status_code, 404)

    def test_link_rejects_bad_ids(self):
        self.assertEqual(self.client.post(self.url("/link"), json={"task": "t new", "pane_id": "w1:p3"}).status_code, 400)
        self.assertEqual(self.client.post(self.url("/link"), json={"task": "t_new", "pane_id": "w1:p3;rm"}).status_code, 400)


class StreamTests(Base):
    def stream(self, pane="w1:p1"):
        return self.client.websocket_connect(self.url("/stream") + f"?pane={pane}")

    def test_initial_snapshot_then_live_updates(self):
        with self.stream() as ws:
            first = ws.receive_json()
            self.assertEqual(first["type"], "snapshot")
            self.assertEqual(first["text"], "one\ntwo\n")
            self.assertEqual(first["agent_status"], "working")
            self.herdr.append("w1:p1", "three\n")
            second = ws.receive_json()
            self.assertTrue(second["text"].endswith("three\n"))
            self.herdr.set_status("w1:p1", "done")
            third = ws.receive_json()
            self.assertEqual(third["agent_status"], "done")

    def test_pane_closing_ends_the_stream(self):
        with self.stream() as ws:
            ws.receive_json()
            self.herdr.close_pane("w1:p1")
            self.herdr.call(lambda: self.herdr.fake.emit("w1:p2"))  # any wake-up; the read then reports not found
            self.herdr.append("w1:p2", "x")
            msg = ws.receive_json()
            while msg["type"] == "snapshot":
                self.herdr.append("w1:p2", "y")
                msg = ws.receive_json()
            self.assertEqual(msg["type"], "closed")

    def test_unknown_pane_reports_closed(self):
        with self.stream("w9:p9") as ws:
            self.assertEqual(ws.receive_json()["type"], "closed")

    def test_bad_pane_id_is_refused(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.stream("bad id;x") as ws:
                ws.receive_json()

    def test_unauthorized_is_refused(self):
        api._ws_authorized = lambda ws: False
        with self.assertRaises(WebSocketDisconnect):
            with self.stream() as ws:
                ws.receive_json()

    def test_too_many_streams(self):
        api.MAX_STREAMS = 1
        try:
            with self.stream() as a:
                a.receive_json()
                with self.stream() as b:
                    msg = b.receive_json()
                    self.assertEqual(msg["type"], "error")
        finally:
            api.MAX_STREAMS = 4

    def test_control_characters_are_removed(self):
        self.herdr.append("w1:p1", "ok\x1b[31m red\x00\x07 bell\n")
        with self.stream() as ws:
            text = ws.receive_json()["text"]
        self.assertNotIn("\x00", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\x1b", text)


class PollingFallbackTests(Base):
    subscribe_works = False

    def test_updates_arrive_by_polling(self):
        with self.client.websocket_connect(self.url("/stream") + "?pane=w1:p1") as ws:
            self.assertEqual(ws.receive_json()["type"], "snapshot")
            self.herdr.append("w1:p1", "polled\n")
            msg = ws.receive_json()
            self.assertTrue(msg["text"].endswith("polled\n"))


class CliTransportMixin:
    """Run the same tests with the Herdr CLI as the transport (what Windows uses: no unix sockets in its Python)."""

    def setUp(self):
        # The Windows dashboard runs on a selector event loop where asyncio subprocesses raise
        # NotImplementedError; the CLI transport must not depend on them.
        patcher = mock.patch.object(asyncio, "create_subprocess_exec", side_effect=NotImplementedError)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["KANBAN_HERDR_TRANSPORT"] = "cli"
        os.environ["HERDR_BIN"] = f'{sys.executable} {HERE / "stub_herdr_cli.py"}'
        super().setUp()

    def tearDown(self):
        super().tearDown()
        os.environ.pop("KANBAN_HERDR_TRANSPORT", None)
        os.environ.pop("HERDR_BIN", None)


class CliPaneListTests(CliTransportMixin, PaneListTests):
    pass


class CliLinkTests(CliTransportMixin, LinkTests):
    pass


class CliStreamTests(CliTransportMixin, StreamTests):
    pass


class CliArgsTests(unittest.TestCase):
    def test_maps_methods_to_cli_calls(self):
        self.assertEqual(api.cli_args("pane.list", {}), ["pane", "list"])
        self.assertEqual(api.cli_args("pane.get", {"pane_id": "w1:p1"}), ["pane", "get", "w1:p1"])
        self.assertEqual(api.cli_args("pane.read", {"pane_id": "w1:p1", "source": "recent", "lines": 50}),
                         ["pane", "read", "w1:p1", "--source", "recent", "--lines", "50", "--format", "text"])
        self.assertEqual(
            api.cli_args("pane.report_metadata", {"pane_id": "w1:p1", "source": "kanban-link", "tokens": {"kanban_task": "t_1", "kanban_board": None}}),
            ["pane", "report-metadata", "w1:p1", "--source", "kanban-link", "--token", "kanban_task=t_1", "--clear-token", "kanban_board"])

    def test_unsupported_method_is_unavailable(self):
        with self.assertRaises(api.HerdrUnavailable):
            api.cli_args("pane.send_text", {"pane_id": "w1:p1"})


class RemoteMachineTests(Base):
    """Panes on other saved machines (herdr --machine <id>) show up beside the local ones."""

    def setUp(self):
        super().setUp()
        self.sock2 = os.path.join(self.tmp, "r.sock")
        self.remote = FakeHerdrThread(self.sock2).start()
        self.remote.add("wR:p1", title="windows exec", agent="codex", status="working", text="remote one\n")
        os.environ["HERDR_BIN"] = f'{sys.executable} {HERE / "stub_herdr_cli.py"}'
        os.environ["HERDR_STUB_MACHINES"] = "m1\tRemote box\thost1\tdefault\tenabled\ndead\tDown box\thost2\tdefault\tenabled\noff\tOff box\thost3\tdefault\tdisabled"
        os.environ["HERDR_STUB_MACHINE_m1"] = self.sock2
        api._machines_cache["at"] = 0.0
        api._remote_cache.clear()
        api.REMOTE_POLL_SECONDS = 0.05
        api.REMOTE_LIST_TTL = 0.0
        api.REMOTE_FAIL_TTL = 0.0
        api.REMOTE_WAIT = 3.0
        api._remote_tasks.clear()

    def tearDown(self):
        self.remote.stop()
        for k in ("HERDR_BIN", "HERDR_STUB_MACHINES", "HERDR_STUB_MACHINE_m1"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_remote_panes_are_listed_with_their_machine(self):
        r = self.client.get(self.url("/all-panes")).json()
        remote = [p for p in r["panes"] if p["machine"]]
        self.assertEqual([(p["pane_id"], p["machine"]) for p in remote], [("m1~wR:p1", "Remote box")])
        self.assertEqual(r["unreachable"], ["Down box"])  # the disabled one is not even tried
        self.assertTrue(any(p["pane_id"] == "w1:p1" for p in r["panes"]))  # local panes still listed

    def test_hanging_machine_does_not_hold_up_the_list(self):
        os.environ["HERDR_STUB_HANG_m1"] = "1"
        self.addCleanup(os.environ.pop, "HERDR_STUB_HANG_m1", None)
        api.REMOTE_TIMEOUT = 1.0
        api.REMOTE_WAIT = 0.3
        api.REMOTE_FAIL_TTL = 60.0
        self.addCleanup(setattr, api, "REMOTE_TIMEOUT", 15.0)
        with self.client as client:  # one event loop for both calls, like the real dashboard
            t0 = time.time()
            r = client.get(self.url("/all-panes")).json()
            self.assertLess(time.time() - t0, 2.0)
            self.assertTrue(any(p["pane_id"] == "w1:p1" for p in r["panes"]))  # local panes still listed
            self.assertIn("Remote box", r["unreachable"])
            time.sleep(1.5)  # the background fetch hits its timeout (and must not hang on the helper's pipes)
            t0 = time.time()
            r = client.get(self.url("/all-panes")).json()
            self.assertLess(time.time() - t0, 0.3)  # remembered as down: answered at once
            self.assertIn("Remote box", r["unreachable"])
            api.REMOTE_FAIL_TTL = 0.0  # retry is due: it starts in the background but is not waited for
            t0 = time.time()
            r = client.get(self.url("/all-panes")).json()
            self.assertLess(time.time() - t0, 0.3)
            self.assertIn("Remote box", r["unreachable"])

    def test_link_remote_pane_tags_it_on_that_machine(self):
        r = self.client.post(self.url("/link"), json={"task": "t_rem", "board": "gsd", "pane_id": "m1~wR:p1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.remote.tokens("wR:p1"), {"kanban_task": "t_rem", "kanban_board": "gsd"})
        self.assertEqual(self.herdr.tokens("w1:p1"), {"kanban_task": "t_a1", "kanban_board": "gsd"})  # local untouched
        panes = self.client.get(self.url("/panes"), params={"task": "t_rem", "board": "gsd"}).json()["panes"]
        self.assertEqual([p["pane_id"] for p in panes], ["m1~wR:p1"])
        self.assertEqual(self.client.post(self.url("/unlink"), json={"task": "t_rem", "board": "gsd", "pane_id": "m1~wR:p1"}).status_code, 200)
        self.assertEqual(self.remote.tokens("wR:p1"), {})

    def test_remote_stream_follows_new_output(self):
        with self.client.websocket_connect(self.url("/stream") + "?pane=m1~wR:p1") as ws:
            self.assertIn("remote one", ws.receive_json()["text"])
            self.remote.append("wR:p1", "remote two\n")
            for _ in range(40):
                m = ws.receive_json()
                if m["type"] == "snapshot" and "remote two" in m["text"]:
                    break
            else:
                self.fail("no live update from the remote pane")

    def test_bad_machine_reference_is_refused(self):
        self.assertEqual(self.client.post(self.url("/link"), json={"task": "t_rem", "pane_id": "a b~w1"}).status_code, 400)
        self.assertNotEqual(self.client.post(self.url("/link"), json={"task": "t_rem", "pane_id": "bad.id~w1:p1"}).status_code, 200)


class CleanTextTests(unittest.TestCase):
    def test_keeps_tail_and_strips_controls(self):
        self.assertEqual(api.clean_text("a\r\nb\rc\x00"), "a\nb\nc")
        big = "x" * (api.MAX_TEXT + 10)
        self.assertEqual(len(api.clean_text(big)), api.MAX_TEXT)


if __name__ == "__main__":
    unittest.main()
