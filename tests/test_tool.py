import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from fake_herdr import FakeHerdrThread  # noqa: E402

spec = importlib.util.spec_from_file_location("kh_tool", HERE.parent / "herdr_tool.py")
tool = importlib.util.module_from_spec(spec)
sys.modules["kh_tool"] = tool
spec.loader.exec_module(tool)


def call(**args):
    return json.loads(tool.handle(args))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="khtool-")
        self.sock = os.path.join(self.tmp, "h.sock")
        self.sock2 = os.path.join(self.tmp, "r.sock")
        self.herdr = FakeHerdrThread(self.sock).start()
        self.remote = FakeHerdrThread(self.sock2).start()
        self.herdr.add("w1:p1", title="pane one", status="idle")
        self.herdr.add("w1:p2", title="pane two", status="idle", tokens={"kanban_task": "t_old", "kanban_board": "gsd"})
        self.remote.add("wR:p1", title="remote", status="idle")
        os.environ["HERDR_SOCKET_PATH"] = self.sock
        os.environ["HERDR_BIN"] = f'{sys.executable} {HERE / "stub_herdr_cli.py"}'
        os.environ["HERDR_STUB_MACHINES"] = "m1\tRemote box\thost1\tdefault\tenabled"
        os.environ["HERDR_STUB_MACHINE_m1"] = self.sock2

    def tearDown(self):
        self.herdr.stop()
        self.remote.stop()
        for k in ("HERDR_SOCKET_PATH", "HERDR_BIN", "HERDR_STUB_MACHINES", "HERDR_STUB_MACHINE_m1"):
            os.environ.pop(k, None)

    def test_link_tags_and_reads_back(self):
        r = call(action="link", pane_id="w1:p1", task_id="t_new", board="connect")
        self.assertTrue(r["ok"] and r["verified"], r)
        self.assertEqual(r["tokens"], {"kanban_task": "t_new", "kanban_board": "connect"})
        self.assertEqual(self.herdr.tokens("w1:p1"), {"kanban_task": "t_new", "kanban_board": "connect"})

    def test_relink_replaces_the_old_card_and_drops_a_stale_board(self):
        r = call(action="link", pane_id="w1:p2", task_id="t_new")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.herdr.tokens("w1:p2"), {"kanban_task": "t_new"})

    def test_show(self):
        r = call(action="show", pane_id="w1:p2")
        self.assertEqual(r["tokens"], {"kanban_task": "t_old", "kanban_board": "gsd"})

    def test_unlink_only_clears_its_own_card(self):
        r = call(action="unlink", pane_id="w1:p2", task_id="t_other")
        self.assertTrue(r["ok"] and r["changed"] is False, r)
        self.assertEqual(self.herdr.tokens("w1:p2")["kanban_task"], "t_old")
        r = call(action="unlink", pane_id="w1:p2", task_id="t_old")
        self.assertTrue(r["ok"] and r["changed"] and r["verified"], r)
        self.assertEqual(self.herdr.tokens("w1:p2"), {})

    def test_unknown_pane_is_a_clear_error(self):
        r = call(action="link", pane_id="w9:p9", task_id="t_new")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "pane_not_found")

    def test_bad_arguments_are_refused_without_touching_herdr(self):
        for args in ({"action": "link", "pane_id": "w1:p1"},                       # no task
                     {"action": "link", "pane_id": "w1:p1; rm -rf /", "task_id": "t_x"},
                     {"action": "link", "pane_id": "w1:p1", "task_id": "t x"},
                     {"action": "link", "pane_id": "w1:p1", "task_id": "t_x", "board": "a b"},
                     {"action": "send", "pane_id": "w1:p1"}):
            r = call(**args)
            self.assertFalse(r["ok"], args)
            self.assertEqual(r["error"], "invalid_argument", args)
        self.assertEqual(self.herdr.tokens("w1:p1"), {})

    def test_machine_by_label_or_id(self):
        for m in ("Remote box", "m1"):
            r = call(action="link", pane_id="wR:p1", task_id="t_rem", board="p5", machine=m)
            self.assertTrue(r["ok"], r)
        self.assertEqual(self.remote.tokens("wR:p1"), {"kanban_task": "t_rem", "kanban_board": "p5"})
        self.assertEqual(self.herdr.tokens("w1:p1"), {})
        self.assertEqual(call(action="show", pane_id="wR:p1", machine="Nope")["error"], "unknown_machine")

    def test_missing_herdr_never_raises(self):
        os.environ["HERDR_BIN"] = os.path.join(self.tmp, "no-such-herdr")
        r = call(action="show", pane_id="w1:p1")
        self.assertFalse(r["ok"])
        self.assertIn(r["error"], ("herdr_not_found", "no_answer"))

    def test_registers_one_read_only_tool(self):
        seen = {}

        class Ctx:
            def register_tool(self, **kw):
                seen.update(kw)

        tool.register(Ctx())
        self.assertEqual(seen["name"], "kanban_herdr_link")
        self.assertEqual(seen["toolset"], "kanban_herdr")
        self.assertEqual(seen["schema"]["parameters"]["properties"]["action"]["enum"], ["link", "unlink", "show"])
        self.assertTrue(seen["check_fn"]())
        tool.register(object())  # a Hermes without register_tool is fine


if __name__ == "__main__":
    unittest.main()
