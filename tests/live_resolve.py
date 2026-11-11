"""Live checks against a REAL DaVinci Resolve. Not part of the automated tests.

Requires Resolve open with a project and the bridge running. Everything happens on a new scratch timeline
named _RCE_LIVE_<time> and a bin named _RCE_LIVE_CAPTIONS; your other timelines are compared before and after.
The scratch timeline is deleted at the end unless --keep is given. Run: python tests/live_resolve.py
"""
import json, os, pathlib, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import editor_tools as E
import mcp_server as M

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  :: {str(detail)[:400]}"))


def B(op, **a):
    return E._b(op, **a)


def status(r):
    return r.get("_operation", {}).get("status")


def main():
    keep = "--keep" in sys.argv
    folder = os.path.join(M.DIR, "validation")
    os.makedirs(folder, exist_ok=True)
    clip_path = os.path.join(folder, "rce_live_clip.mp4")
    if not os.path.exists(clip_path):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi",
                        "-i", "sine=frequency=440:sample_rate=48000:d=12", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", clip_path], check=True)
    state = B("get_state")
    original = sys.argv[sys.argv.index("--return-to") + 1] if "--return-to" in sys.argv else state["timeline"]
    B("set_current_timeline", name=original)
    before = (B("list_timeline_items", track_type="video"), B("list_timeline_items", track_type="audio"))
    name = "_RCE_LIVE_" + time.strftime("%H%M%S")
    try:
        media = [m for m in B("list_media") if m["name"] == "rce_live_clip.mp4"]
        clip = media[0]["id"] if media else B("import_media", paths=[clip_path])[0]["id"]
        B("create_timeline", name=name)
        B("set_current_timeline", name=name)
        E.invalidate()
        start = B("get_state")["timeline_start_frame"]
        guard = lambda: B("get_state")["timeline"] == name or sys.exit("wrong timeline, stopping")

        r = E.run("place_clips", {"clips": [{"clip": clip, "start_frame": 30, "end_frame": 90, "record": start},
                                            {"clip": clip, "start_frame": 120, "end_frame": 180, "record": start + 60}]})
        check("place_clips previews first", status(r) == "preview", r)
        guard()
        r = E.run("place_clips", {"clips": [{"clip": clip, "start_frame": 30, "end_frame": 90, "record": start},
                                            {"clip": clip, "start_frame": 120, "end_frame": 180, "record": start + 60}],
                                  "confirm": True})
        check("place_clips with linked audio", status(r) == "success", r)
        r = E.run("place_clips", {"clips": [{"clip": clip, "start_frame": 0, "end_frame": 10, "record": start + 5}],
                                  "confirm": True})
        check("place_clips refuses overwrite", status(r) == "blocked", r)

        video = sorted([x for x in E._items() if x["track_type"] == "video"], key=lambda x: x["start"])
        first, second = video[0], video[1]
        B("set_item_properties", item_id=first["id"], properties={"ZoomX": 1.4, "ZoomY": 1.4, "Opacity": 75.0})
        B("set_grade", item_ids=[first["id"]], slope="1.1 1.0 0.9", saturation=1.2)
        B("set_fades", item_id=first["id"], fade_in=6, fade_out=0)
        B("review_item", item_id=first["id"], color="Orange", flag="Blue")
        audio_of_first = [x for x in E._items() if x["track_type"] == "audio" and x["start"] == first["start"]][0]
        r = E.run("set_audio", {"item_ids": [audio_of_first["id"]], "volume_db": -6})
        check("set_audio read-back", status(r) == "success", r)

        r = E.run("move_item", {"item_id": first["id"], "record": start + 200})
        check("move preview lists linked audio", status(r) == "preview" and len(r["plan"]["move"]) == 2, r)
        guard()
        r = E.run("move_item", {"item_id": first["id"], "record": start + 200, "confirm": True})
        check("move keeps settings and verifies", status(r) == "success", r)
        moved = [c["new_id"] for c in r.get("_operation", {}).get("changes", {}).get("replaced", [])]
        details = {d["id"]: d for d in B("item_details", item_ids=moved)} if moved else {}
        vid = next((d for d in details.values() if d.get("type") == "video"), {})
        aud = next((d for d in details.values() if d.get("type") == "audio"), {})
        check("moved video kept zoom/opacity/fade/colour",
              (vid.get("properties") or {}).get("ZoomX") == 1.4 and (vid.get("properties") or {}).get("Opacity") == 75
              and int((vid.get("fades") or {}).get("FadeIn", 0)) == 6 and vid.get("color") == "Orange", vid)
        check("moved audio kept volume", abs(float((aud.get("properties") or {}).get("AudioVolume", 0)) + 6) < 0.01, aud)
        check("moved clips relinked", set(vid.get("linked") or []) == {aud.get("id")}, vid.get("linked"))
        probe = B("probe_color", item_id=vid["id"]) if vid else {}
        check("moved video still has its grade node", (probe or {}).get("item_nodes", 0) >= 1, probe)
        check("staging tracks removed", B("get_state") and len({x["track"] for x in E._items()
                                                                 if x["track_type"] == "video"}) == 1)

        r = E.run("trim_item", {"item_id": vid["id"], "head_frames": 5, "tail_frames": 10, "confirm": True})
        trimmed = (r.get("_operation", {}).get("changes", {}).get("replaced") or [{}])[0]
        check("trim preserves and verifies", status(r) == "success" and trimmed.get("to", [None, 0])[1] == 45, r)

        r = E.run("add_transition", {"item_id": second["id"], "position": "start", "duration": 10})
        check("transition refused or added honestly", status(r) in ("success", "partial") or "refused" in str(r), r)
        r = E.run("set_fades", {"item_ids": [second["id"]], "fade_in": 0, "fade_out": 8})
        check("set_fades read-back", status(r) == "success", r)
        r = E.run("set_enabled", {"item_ids": [second["id"]], "enabled": False})
        check("disable clip read-back", status(r) == "success", r)
        E.run("set_enabled", {"item_ids": [second["id"]], "enabled": True})

        r = E.run("add_markers", {"markers": [{"at": start + 15, "name": "a"}, {"at": E._tc(start + 30), "name": "b"}]})
        check("markers by frame and timecode", status(r) == "success" and set(B("list_markers")) >= {"15", "30"}, r)
        r = E.run("add_audio_markers", {"path": clip_path, "kind": "beats", "dry_run": True})
        check("beat analysis runs", status(r) == "planned", r)

        segs = [{"start": 0.5, "end": 2.0, "text": "First caption"}, {"start": 2.2, "end": 9.0, "text": "A long second one"}]
        r = E.run("add_captions", {"segments": segs, "confirm": True, "folder": "_RCE_LIVE_CAPTIONS"})
        cards = [x for x in E._items() if x["track_type"] == "video" and x["track"] == max(
            x["track"] for x in E._items() if x["track_type"] == "video")]
        check("captions placed with exact lengths", status(r) == "success", r)
        check("caption lengths 45/150/54", sorted(x["duration"] for x in cards) == [45, 54, 150], cards)

        r = E.run("timeline_summary", {})
        check("summary counts transitions separately", "transitions" in r and r["overlaps"]["count"] == 0, r)
        r = E.run("inspect_range", {"start": start, "end": start + 400, "details": True})
        check("inspect_range details", r["count"] > 0, r)
        out = os.path.join(folder, f"rce_live_{int(time.time())}.otio")
        r = E.run("export_timeline", {"format": "OTIO", "output": out})
        check("OTIO export", status(r) == "success", r)
    except SystemExit:
        raise
    except Exception as exc:
        check("live run aborted", False, exc)
    finally:
        if original.startswith("_RCE_"):  # never leave the user on a scratch timeline
            original = next((t for t in B("list_timelines") if not t.startswith("_RCE_")), original)
        B("set_current_timeline", name=original)
        E.invalidate()
        after = (B("list_timeline_items", track_type="video"), B("list_timeline_items", track_type="audio"))
        check("your current timeline is unchanged", after == before)
        if not keep:
            for t in [t for t in B("list_timelines") if t.startswith(name)]:
                print("cleanup", t, B("delete_scratch", timeline=t))
            print("cleanup bin", B("delete_scratch", folder="_RCE_LIVE_CAPTIONS"))
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
