import json, os, pathlib, subprocess, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RESOLVE_BRIDGE_DIR"] = tempfile.mkdtemp()
import editor_tools as E
import mcp_server as M


def item(id, start, dur, name="a.mp4", src=0, track=1, kind="video"):
    return {"id": id, "name": name, "start": start, "end": start + dur, "duration": dur, "source_start": src,
            "track": track, "track_type": kind}


class FakeResolve:
    def __init__(self, items, fail_append=0):
        self.items, self.fail_append, self.calls, self.n = items, fail_append, [], 0

    def __call__(self, op, args, **kw):
        self.calls.append(op)
        r = None
        if op == "list_timeline_items":
            r = [x for x in self.items if x["track_type"] == args["track_type"]]
        elif op == "list_media":
            r = [{"id": "c1", "name": "a.mp4", "fps": 30, "frames": "300"}]
        elif op == "get_state":
            r = {"timeline": "T", "timeline_fps": 30, "timeline_start_frame": 1000}
        elif op == "delete_items":
            self.items[:] = [x for x in self.items if x["id"] not in args["item_ids"]]
            r = {"ok": True}
        elif op == "append_clips":
            if self.fail_append > 0:
                self.fail_append -= 1
                return {"ok": False, "error": "boom"}
            c = args["clips"][0]
            self.n += 1
            self.items.append(item(f"n{self.n}", c["record_frame"], c["end_frame"] - c["start_frame"],
                                   src=c["start_frame"], track=c["track_index"]))
            r = []
        elif op == "add_text_plus":
            r = {"items": args["items"]}
        elif op == "add_marker":
            r = {"ok": True}
        return {"ok": True, "result": r}


class EditorTests(unittest.TestCase):
    def use(self, fake):
        old = M.call_bridge
        M.call_bridge = fake
        self.addCleanup(setattr, M, "call_bridge", old)
        return fake

    def test_analyze(self):
        r = E.analyze([item("a", 0, 10), item("b", 20, 10), item("c", 25, 10, name="z")])
        self.assertEqual(r["gaps"][0]["frames"], 10)
        self.assertEqual(r["overlaps"][0]["frames"], 5)
        self.assertEqual(r["reused_clips"], {"a.mp4": [0, 20]})

    def test_confirm_required(self):
        f = self.use(FakeResolve([item("a", 0, 10)]))
        self.assertEqual(E.run("move_item", {"item_id": "a", "record_frame": 50})["_operation"]["status"], "blocked")
        self.assertEqual(f.calls, [])

    def test_move(self):
        f = self.use(FakeResolve([item("a", 0, 10, src=5)]))
        r = E.run("move_item", {"item_id": "a", "record_frame": 50, "confirm": True})
        self.assertEqual(r["_operation"]["verification"], "passed")
        self.assertEqual((f.items[0]["start"], f.items[0]["source_start"]), (50, 5))

    def test_collision_touches_nothing(self):
        f = self.use(FakeResolve([item("a", 0, 10), item("b", 50, 10)]))
        r = E.run("move_item", {"item_id": "a", "record_frame": 45, "confirm": True})
        self.assertEqual(r["_operation"]["status"], "blocked")
        self.assertNotIn("delete_items", f.calls)

    def test_failed_append_restores(self):
        f = self.use(FakeResolve([item("a", 0, 10, src=5)], fail_append=1))
        r = E.run("move_item", {"item_id": "a", "record_frame": 50, "confirm": True})
        self.assertEqual(r["_operation"]["status"], "failed")
        self.assertEqual((f.items[0]["start"], f.items[0]["source_start"]), (0, 5))

    def test_trim(self):
        f = self.use(FakeResolve([item("a", 100, 60, src=10)]))
        E.run("trim_item", {"item_id": "a", "head_frames": 5, "tail_frames": 10, "confirm": True})
        x = f.items[0]
        self.assertEqual((x["start"], x["duration"], x["source_start"]), (105, 45, 15))

    def test_trim_past_source_blocked(self):
        f = self.use(FakeResolve([item("a", 100, 60, src=0)]))
        r = E.run("trim_item", {"item_id": "a", "head_frames": -5, "confirm": True})
        self.assertEqual(r["_operation"]["status"], "blocked")
        self.assertNotIn("delete_items", f.calls)

    def test_captions_frames(self):
        self.use(FakeResolve([]))
        r = E.run("add_captions", {"segments": [{"start": 1, "end": 2.5, "text": " hi "}], "dry_run": True})
        self.assertEqual(r["captions"], [{"text": "hi", "record_frame": 1030, "frames": 45}])

    def test_srt(self):
        self.assertEqual(E.to_srt([{"start": 61.5, "end": 62, "text": "hi"}]),
                         "1\n00:01:01,500 --> 00:01:02,000\nhi\n\n")

    def test_onsets(self):
        rate, s = 11025, []
        for _ in range(4):
            s += [0.8] * 1000 + [0.0] * (rate // 2 - 1000)
        self.assertGreaterEqual(len(E.onsets(s, rate)), 3)


class GatewayResilience(unittest.TestCase):
    def rpc(self, env):
        p = subprocess.Popen([sys.executable, str(ROOT / "unified_server.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
        self.addCleanup(lambda: (p.terminate(), p.wait(5), p.stdin.close(), p.stdout.close()))

        def call(i, m, params=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": params or {}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())
        return call

    def test_runs_without_upstream_and_guards(self):
        env = dict(os.environ, RESOLVE_MCP_REPO=tempfile.mkdtemp(), RESOLVE_MCP_SERVER="missing.py",
                   RESOLVE_MCP_PYTHON="missing.exe")
        env.pop("RESOLVE_MCP_COMMAND", None)
        call = self.rpc(env)
        self.assertIn("instructions", call(1, "initialize", {"protocolVersion": "2025-03-26"})["result"])
        names = {t["name"] for t in call(2, "tools/list")["result"]["tools"]}
        self.assertIn("edit__move_item", names)
        self.assertIn("legacy__get_state", names)
        self.assertFalse([n for n in names if n.startswith("resolve__")])
        r = call(3, "tools/call", {"name": "legacy__start_render", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("confirm", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
