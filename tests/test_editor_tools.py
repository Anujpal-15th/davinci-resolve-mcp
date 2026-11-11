"""Automated tests for edit__ tools against a fake Resolve. They need no Resolve installation.

These prove the logic (planning, refusals, rollback order, frame maths). They cannot prove that Resolve itself
behaves the same way; see tests/live_resolve.py for the checks that need a real Resolve.
"""
import json, os, pathlib, subprocess, sys, tempfile, unittest
from fractions import Fraction

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["RESOLVE_BRIDGE_DIR"] = tempfile.mkdtemp()
import editor_tools as E
import mcp_server as M


def item(id, start, dur, name="a.mp4", src=0, track=1, kind="video", type=None):
    return {"id": id, "name": name, "start": start, "end": start + dur, "duration": dur, "source_start": src,
            "track": track, "track_type": kind, "type": type or kind}


class FakeResolve:
    """Just enough of the bridge to exercise edit__ logic. fail_on makes one op fail once."""

    def __init__(self, items, fail_on=None, details=None, fps=30):
        self.items, self.fail_on, self.calls, self.n, self.fps = items, dict(fail_on or {}), [], 0, fps
        self.tracks, self.timelines = {"video": 1, "audio": 1}, ["T"]
        self.settings = {x["id"]: {"properties": {"ZoomX": 1.5}, "fades": {"FadeIn": 4, "FadeOut": 0}, "color": "Orange",
                                   "enabled": True, "speed": {"Percentage": 100}, "linked": [], "fusion_comps": 0,
                                   "takes": 0, "media_id": "c1"} for x in items}
        for k, v in (details or {}).items():
            self.settings[k].update(v)

    def __call__(self, op, args, **kw):
        self.calls.append(op)
        if self.fail_on.get(op):
            self.fail_on[op] -= 1
            return {"ok": False, "error": f"{op} failed"}
        r = None
        if op == "list_timeline_items":
            r = [x for x in self.items if x["track_type"] == args["track_type"]]
        elif op == "list_media":
            r = [{"id": "c1", "name": "a.mp4", "fps": self.fps, "frames": "300"}]
        elif op == "get_state":
            r = {"timeline": "T", "timeline_fps": 30, "timeline_start_frame": 1000, "timeline_end_frame": 2000,
                 "timeline_drop_frame": "0"}
        elif op == "item_details":
            r = [dict(self.settings[i], id=i) for i in args["item_ids"]]
        elif op == "add_track":
            self.tracks[args["track_type"]] += 1
            r = {"count": self.tracks[args["track_type"]], "ok": True}
        elif op == "delete_empty_track":
            self.tracks[args["track_type"]] -= 1
            r = {"ok": True}
        elif op == "delete_items":
            self.items[:] = [x for x in self.items if x["id"] not in args["item_ids"]]
            r = {"ok": True}
        elif op == "append_clips":
            for c in args["clips"]:
                self.n += 1
                kind = "audio" if c.get("media_type") == 2 else "video"
                new = item(f"n{self.n}", c["record_frame"], c["end_frame"] - c["start_frame"], src=c["start_frame"],
                           track=c["track_index"], kind=kind)
                self.items.append(new)
                self.settings[new["id"]] = {"properties": {"ZoomX": 1}, "fades": {"FadeIn": 0, "FadeOut": 0}, "color": "",
                                            "enabled": True, "speed": {"Percentage": 100}, "linked": [], "media_id": "c1"}
            r = []
        elif op == "copy_settings":
            src, dst = self.settings[args["from_id"]], self.settings[args["to_id"]]
            for k in ("properties", "fades", "color", "enabled"):
                dst[k] = json.loads(json.dumps(src[k]))
            r = {"copied": ["grade"], "failed": []}
        elif op == "duplicate_timeline":
            self.timelines.append(args["name"])
            r = {"name": args["name"]}
        elif op in ("link_items", "set_current_timeline"):
            r = {"ok": True}
        elif op == "list_timelines":
            r = list(self.timelines)
        elif op == "add_markers":
            r = {"added": len(args["markers"])}
        elif op == "add_title_cards":
            r = {"placed": [item(f"t{i}", c["record_frame"], c["frames"], name="Text+") for i, c in enumerate(args["cards"])]}
        return {"ok": True, "result": r}


class Base(unittest.TestCase):
    def use(self, fake):
        old = M.call_bridge
        M.call_bridge = fake
        E.invalidate()
        self.addCleanup(setattr, M, "call_bridge", old)
        self.addCleanup(E.invalidate)
        return fake


class Timecode(unittest.TestCase):
    def test_fps(self):
        self.assertEqual(E.parse_fps(30), 30)
        self.assertEqual(E.parse_fps("29.97"), Fraction(30000, 1001))
        self.assertEqual(E.parse_fps("23.976"), Fraction(24000, 1001))

    def test_round_trip(self):
        for fps, drop in ((30, False), (Fraction(30000, 1001), True), (Fraction(24000, 1001), False), (25, False)):
            for frame in (0, 1, 1799, 1800, 17982, 107892, 108000, 215784):
                self.assertEqual(E.tc_to_frame(E.frame_to_tc(frame, fps, drop), fps, drop), frame, (fps, drop, frame))

    def test_drop_frame_known_values(self):
        df = Fraction(30000, 1001)
        self.assertEqual(E.frame_to_tc(1800, df, True), "00:01:00;02")
        self.assertEqual(E.frame_to_tc(17982, df, True), "00:10:00;00")
        self.assertEqual(E.frame_to_tc(108000, 30), "01:00:00:00")

    def test_seconds_to_frames_ntsc(self):
        self.assertEqual(E.seconds_to_frames(1.001, Fraction(30000, 1001)), 30)


class Inspection(Base):
    def test_summary_ignores_transitions_and_is_compact(self):
        items = [item("a", 1000, 10), item("t", 1005, 10, name="Cross Dissolve", type="transition"), item("b", 1010, 10)]
        items += [item(f"g{i}", 1100 + i * 20, 10, name=f"x{i}") for i in range(30)]
        self.use(FakeResolve(items))
        r = E.run("timeline_summary", {"limit": 3})
        self.assertEqual(r["transitions"], 1)
        self.assertEqual(r["overlaps"]["count"], 0)
        self.assertEqual(len(r["gaps"]["first"]), 3)
        self.assertEqual(r["gaps"]["first"][0]["at"], "00:00:34:00")  # frame 1020 at 30 fps

    def test_reused_clips(self):
        r = E.analyze([item("a", 0, 10), item("b", 20, 10), item("c", 25, 10, name="z")])
        self.assertEqual(r["reused_clips"], {"a.mp4": ["0", "20"]})

    def test_cache_avoids_repeat_reads(self):
        f = self.use(FakeResolve([item("a", 1000, 10)]))
        E.run("timeline_summary", {})
        E.run("timeline_summary", {})
        self.assertEqual(f.calls.count("list_timeline_items"), 2)  # video + audio once, not twice per call


class Replace(Base):
    def test_preview_changes_nothing(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)]))
        r = E.run("move_item", {"item_id": "a", "record": 1050})
        self.assertEqual(r["_operation"]["status"], "preview")
        self.assertFalse({"delete_items", "append_clips", "add_track"} & set(f.calls))

    def test_move_keeps_settings(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)]))
        r = E.run("move_item", {"item_id": "a", "record": "00:00:35:00", "confirm": True})
        self.assertEqual(r["_operation"]["verification"], "passed", r)
        moved = [x for x in f.items if x["track"] == 1]
        self.assertEqual([(x["start"], x["source_start"]) for x in moved], [(1050, 5)])
        self.assertEqual(f.settings[moved[0]["id"]]["properties"], {"ZoomX": 1.5})
        self.assertEqual(f.tracks["video"], 1, "staging track removed")

    def test_backup_made_before_edit(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)]))
        r = E.run("move_item", {"item_id": "a", "record": 1050, "confirm": True})
        self.assertTrue(r["snapshot"].startswith("T before edit"))
        self.assertLess(f.calls.index("duplicate_timeline"), f.calls.index("add_track"))

    def test_original_deleted_only_after_replacement_is_ready(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)]))
        E.run("move_item", {"item_id": "a", "record": 1050, "confirm": True, "backup": False})
        self.assertLess(f.calls.index("copy_settings"), f.calls.index("delete_items"))

    def test_staging_failure_leaves_original(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)], fail_on={"copy_settings": 1}))
        r = E.run("move_item", {"item_id": "a", "record": 1050, "confirm": True, "backup": False})
        self.assertEqual(r["_operation"]["status"], "failed")
        self.assertIn("a", [x["id"] for x in f.items])
        self.assertEqual(f.tracks["video"], 1)

    def test_swap_failure_rebuilds_original_with_settings(self):
        class FailSecondAppend(FakeResolve):
            def __call__(self, op, args, **kw):
                if op == "append_clips":
                    self.appends = getattr(self, "appends", 0) + 1
                    if self.appends == 2:
                        return {"ok": False, "error": "refused"}
                return super().__call__(op, args, **kw)
        f = self.use(FailSecondAppend([item("a", 1000, 10, src=5)]))
        r = E.run("move_item", {"item_id": "a", "record": 1050, "confirm": True, "backup": False})
        self.assertEqual(r["_operation"]["status"], "failed")
        back = [x for x in f.items if x["track"] == 1]
        self.assertEqual([(x["start"], x["source_start"], x["duration"]) for x in back], [(1000, 5, 10)])
        self.assertEqual(f.settings[back[0]["id"]]["properties"], {"ZoomX": 1.5})

    def test_trim_moves_linked_audio_too(self):
        items = [item("v", 1000, 60, src=10), item("a", 1000, 60, src=10, kind="audio")]
        f = self.use(FakeResolve(items, details={"v": {"linked": ["a"]}, "a": {"linked": ["v"]}}))
        r = E.run("trim_item", {"item_id": "v", "head_frames": 5, "tail_frames": 10, "confirm": True, "backup": False})
        self.assertEqual(r["_operation"]["status"], "success", r)
        got = sorted((x["track_type"], x["start"], x["duration"], x["source_start"]) for x in f.items if x["track"] == 1)
        self.assertEqual(got, [("audio", 1005, 45, 15), ("video", 1005, 45, 15)])
        self.assertIn("link_items", f.calls)

    def test_refusals(self):
        cases = [({"speed": {"Percentage": 50}}, "speed"), ({"fusion_comps": 1}, "Fusion"), ({"media_id": None}, "media-pool")]
        for detail, word in cases:
            self.use(FakeResolve([item("a", 1000, 10)], details={"a": detail}))
            r = E.run("move_item", {"item_id": "a", "record": 1050, "confirm": True})
            self.assertEqual(r["_operation"]["status"], "blocked")
            self.assertIn(word, r["reason"])

    def test_mixed_fps_refused(self):
        self.use(FakeResolve([item("a", 1000, 10)], fps=25))
        self.assertIn("frame rate", E.run("move_item", {"item_id": "a", "record": 1050})["reason"])

    def test_collision_and_transition_refused(self):
        self.use(FakeResolve([item("a", 1000, 10), item("b", 1050, 10)]))
        self.assertIn("overwrite", E.run("move_item", {"item_id": "a", "record": 1045})["reason"])
        self.use(FakeResolve([item("a", 1000, 10), item("t", 1005, 10, type="transition")]))
        self.assertIn("transition", E.run("move_item", {"item_id": "a", "record": 1100})["reason"])

    def test_trim_past_source(self):
        self.use(FakeResolve([item("a", 1000, 60, src=0)]))
        self.assertIn("outside", E.run("trim_item", {"item_id": "a", "head_frames": -5})["reason"])


class Captions(Base):
    def test_split_and_no_overlap(self):
        segs = E.split_segments([{"start": 0, "end": 4, "text": "one two three four five six seven eight nine ten"},
                                 {"start": 3.5, "end": 5, "text": "next"}], max_chars=20)
        self.assertTrue(all(len(s["text"]) <= 20 for s in segs))
        for a, b in zip(segs, segs[1:]):
            self.assertLessEqual(a["end"], b["start"])

    def test_cards_cap_at_five_seconds(self):
        cards = E.caption_cards([{"start": 0, "end": 7, "text": "long"}], 30, 1000, "T")
        self.assertEqual([(c["record_frame"], c["frames"]) for c in cards], [(1000, 150), (1150, 60)])
        self.assertEqual(len({c["name"] for c in cards}), 2)

    def test_add_captions_preview_then_apply(self):
        f = self.use(FakeResolve([]))
        segs = [{"start": 1, "end": 2.5, "text": "hi"}]
        r = E.run("add_captions", {"segments": segs})
        self.assertEqual(r["_operation"]["status"], "preview")
        self.assertNotIn("add_title_cards", f.calls)
        r = E.run("add_captions", {"segments": segs, "confirm": True})
        self.assertEqual(r["_operation"]["verification"], "passed", r)

    def test_srt(self):
        self.assertEqual(E.to_srt([{"start": 61.5, "end": 62, "text": "hi"}]), "1\n00:01:01,500 --> 00:01:02,000\nhi\n\n")


class Analysis(Base):
    def test_onsets(self):
        rate, s = 11025, []
        for _ in range(4):
            s += [0.8] * 1000 + [0.0] * (rate // 2 - 1000)
        self.assertGreaterEqual(len(E.onsets(s, rate)), 3)

    def test_qc_parse(self):
        log = ("[blackdetect] black_start:0 black_end:0.8\n[freezedetect] freeze_start: 3.2\n"
               "  Integrated loudness:\n    I:         -14.2 LUFS\n  Loudness range:\n    LRA:         5.1 LU\n"
               "  True peak:\n    Peak:       -0.5 dBFS\n")
        r = E.parse_qc(log)
        self.assertEqual((r["integrated_lufs"], r["true_peak_dbfs"], r["black_segments"]), (-14.2, -0.5, [[0.0, 0.8]]))

    def test_batch_previews_without_writing(self):
        f = self.use(FakeResolve([item("a", 1000, 10, src=5)]))
        r = E.run("batch", {"operations": [{"tool": "move_item", "args": {"item_id": "a", "record": 1050}}]})
        self.assertEqual(r["_operation"]["status"], "preview")
        self.assertNotIn("delete_items", f.calls)


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
        tools = call(2, "tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertTrue({"edit__move_item", "edit__timeline_summary", "legacy__get_state"} <= names)
        self.assertFalse([n for n in names if n.startswith("resolve__")])
        self.assertLess(len(json.dumps(tools)), 60000, "tool list stays small enough for context")
        r = call(3, "tools/call", {"name": "legacy__start_render", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("confirm", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
