"""High-level edit__ tools built on the Lua bridge and FFmpeg.

Each mutating tool checks first, acts, then reads the timeline back. Results carry an `_operation` block
(status, verification, changes, warnings), an idea borrowed from the upstream Resolve MCP.
"""
import array
import json
import math
import os
import shutil
import statistics
import subprocess
import time

import mcp_server as M

S, I, B, N = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
SEGMENTS = {"type": "array", "items": {"type": "object", "properties": {"start": N, "end": N, "text": S},
                                       "required": ["start", "end", "text"]},
            "description": "Caption lines in seconds. Omit to transcribe `path` instead."}
AUDIT = os.path.join(M.DIR, "audit.jsonl")


def T(name, desc, props=None, req=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(req)}}


CONFIRM = {"type": "boolean", "description": "Must be true. Ask the user first."}
TOOLS = [
    T("doctor", "Check Python, FFmpeg, transcription, the upstream engine and the live Resolve bridge."),
    T("timeline_report", "Read-only health check of the current timeline: gaps, overlaps, and source clips used more "
      "than once. Run this before changing a timeline, since the user may have edited it by hand."),
    T("snapshot_timeline", "Duplicate the current timeline as a timestamped backup and stay on the original.",
      {"label": S}),
    T("move_item", "Move one timeline item to a new position or track. Internally delete + re-append, so transforms "
      "and grades on that item reset. Refuses collisions, titles and mixed frame rates; restores on failure.",
      {"item_id": S, "record_frame": I, "track_index": I, "confirm": CONFIRM}, ["item_id", "record_frame", "confirm"]),
    T("trim_item", "Trim one timeline item. Positive frames shorten that end, negative extend it. Internally delete + "
      "re-append, so transforms and grades on that item reset. Restores on failure.",
      {"item_id": S, "head_frames": I, "tail_frames": I, "confirm": CONFIRM}, ["item_id", "confirm"]),
    T("add_audio_markers", "Find silences or musical onsets in a file and mark them on the timeline. dry_run only "
      "lists them. Measurements, not edit decisions.",
      {"path": S, "kind": {"type": "string", "enum": ["silence", "beats"]}, "timeline_offset_frames": I,
       "color": S, "noise_db": N, "min_duration": N, "max_markers": I, "dry_run": B}, ["path", "kind"]),
    T("add_captions", "Add editable Text+ captions from segments or by transcribing `path`. record_frame is where the "
      "audio starts (default: timeline start). dry_run only lists them.",
      {"path": S, "segments": SEGMENTS, "record_frame": I, "model": S, "language": S, "font": S, "size": N, "y": N,
       "lock_video_tracks": {"type": "array", "items": I}, "dry_run": B}),
    T("export_srt", "Write an .srt subtitle file from segments or by transcribing `path`.",
      {"path": S, "segments": SEGMENTS, "output": S, "model": S, "language": S}),
    T("audit_log", "Recent changes made through this server.", {"limit": I}),
]
NAMES = {t["name"] for t in TOOLS}


def log_audit(tool, args, status):
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "args": args,
                                "status": status}, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _op(status, verified=None, changes=None, warnings=(), **payload):
    verification = "unverified" if verified is None else ("passed" if verified else "contradiction")
    payload["_operation"] = {"status": status, "verification": verification, "changes": changes or {},
                             "warnings": list(warnings)}
    return payload


def _blocked(reason):
    return _op("blocked", reason=reason)


def _b(op, **args):
    res = M.call_bridge(op, args)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "bridge error"))
    return res.get("result")


def _list(value):
    return value if isinstance(value, list) else []


def _items():
    return _list(_b("list_timeline_items", track_type="video")) + _list(_b("list_timeline_items", track_type="audio"))


def analyze(items):
    """Pure timeline analysis: gaps, overlaps, reused source clips."""
    tracks, gaps, overlaps, used = {}, [], [], {}
    for it in items:
        tracks.setdefault((it["track_type"], it["track"]), []).append(it)
        if it["track_type"] == "video":
            used.setdefault(it["name"], []).append(it["start"])
    for (kind, track), row in sorted(tracks.items()):
        row.sort(key=lambda x: x["start"])
        for a, b in zip(row, row[1:]):
            if b["start"] > a["end"]:
                gaps.append({"track_type": kind, "track": track, "start": a["end"], "frames": b["start"] - a["end"]})
            elif b["start"] < a["end"]:
                overlaps.append({"track_type": kind, "track": track, "a": a["id"], "b": b["id"],
                                 "frames": a["end"] - b["start"]})
    return {"item_count": len(items), "tracks": {f"{k}{t}": len(r) for (k, t), r in sorted(tracks.items())},
            "gaps": gaps, "overlaps": overlaps,
            "reused_clips": {n: s for n, s in sorted(used.items()) if len(s) > 1}}


def _prepare(item_id):
    """Return (item, items, clip, fps), or a reason string if replacing this item would be unsafe."""
    items = _items()
    item = next((x for x in items if x["id"] == item_id), None)
    if not item:
        return f"timeline item not found: {item_id}"
    if item.get("source_start") is None:
        return "Resolve did not report this item's source offset"
    clips = [c for c in _list(_b("list_media")) if c.get("name") == item["name"]]
    if len(clips) != 1:
        return f"need exactly one media-pool clip named {item['name']!r}, found {len(clips)} (titles can't be moved)"
    state = _b("get_state")
    fps = float(state.get("timeline_fps") or 0)
    if not fps or abs(float(clips[0].get("fps") or 0) - fps) > 0.01:
        return f"clip fps {clips[0].get('fps')} differs from timeline fps {fps}; source frames would not line up"
    return item, items, clips[0], fps


def _append(item, clip, first, last, record, track):
    _b("append_clips", clips=[{"clip": clip["id"], "start_frame": first, "end_frame": last, "record_frame": record,
                               "track_index": track, "media_type": 1 if item["track_type"] == "video" else 2}])


def _replace(item, items, clip, first, last, record, track):
    frames = float(clip.get("frames") or 0)
    if first < 0 or last <= first or (frames and last > frames):
        return _blocked(f"source range {first}-{last} is outside the clip (0-{int(frames)})")
    end = record + last - first
    for other in items:
        if (other["id"] != item["id"] and other["track_type"] == item["track_type"] and other["track"] == track
                and other["start"] < end and record < other["end"]):
            return _blocked(f"would overlap {other['name']} at {other['start']}-{other['end']}")
    old_first = item["source_start"]
    old_last = old_first + item["duration"]
    if not _b("delete_items", item_ids=[item["id"]]).get("ok"):
        return _op("failed", reason="Resolve refused the delete; nothing changed")
    try:
        _append(item, clip, first, last, record, track)
    except Exception as exc:
        _append(item, clip, old_first, old_last, item["start"], item["track"])
        return _op("failed", warnings=["item restored, but its transforms and grade were reset"], reason=str(exc))
    hit = [x for x in _b("list_timeline_items", track_type=item["track_type"])
           if x["track"] == track and x["start"] == record and x["name"] == item["name"]]
    ok = len(hit) == 1 and hit[0]["duration"] == last - first
    return _op("success" if ok else "partial", ok,
               {"from": {"start": item["start"], "track": item["track"], "frames": item["duration"]},
                "to": {"start": record, "track": track, "frames": last - first}},
               ["transforms and grade on this item were reset"], item=hit[0] if hit else None)


def move_item(a):
    prep = _prepare(a["item_id"])
    if isinstance(prep, str):
        return _blocked(prep)
    item, items, clip, _ = prep
    first = item["source_start"]
    return _replace(item, items, clip, first, first + item["duration"], a["record_frame"],
                    a.get("track_index") or item["track"])


def trim_item(a):
    prep = _prepare(a["item_id"])
    if isinstance(prep, str):
        return _blocked(prep)
    item, items, clip, _ = prep
    head, tail = a.get("head_frames") or 0, a.get("tail_frames") or 0
    first = item["source_start"] + head
    return _replace(item, items, clip, first, item["source_start"] + item["duration"] - tail, item["start"] + head,
                    item["track"])


def snapshot_timeline(a):
    current = _b("get_state")["timeline"]
    if not current:
        return _blocked("no current timeline")
    name = f"{current} {a.get('label') or 'backup'} {time.strftime('%Y%m%d-%H%M%S')}"
    _b("duplicate_timeline", name=name)
    _b("set_current_timeline", name=current)
    ok = name in _list(_b("list_timelines")) and _b("get_state")["timeline"] == current
    return _op("success" if ok else "partial", ok, {"created_timeline": name}, snapshot=name, current=current)


def onsets(samples, rate, hop=512, min_gap=0.25):
    """Energy-flux onset times in seconds. Works best on percussive audio."""
    energy = []
    for i in range(0, len(samples) - hop, hop):
        chunk = samples[i:i + hop]
        energy.append(math.log(sum(x * x for x in chunk) / hop + 1e-4))  # floor about -40 dB ignores hiss
    flux = [max(0.0, b - a) for a, b in zip(energy, energy[1:])]
    if len(flux) < 8:
        return []
    window, last, out = max(8, int(rate / hop)), -min_gap, []
    for i, value in enumerate(flux):
        local = flux[max(0, i - window):i + window]
        t = (i + 1) * hop / rate
        if value > statistics.mean(local) + 1.5 * statistics.pstdev(local) and value > 0.5 and t - last >= min_gap:
            out.append(round(t, 3))
            last = t
    return out


def _beats(path):
    rate = 11025
    ffmpeg = M._binary("ffmpeg", "FFMPEG")
    proc = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", M._media_path(path), "-map", "0:a:0", "-ac", "1",
                           "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, timeout=600)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-2000:])
    samples = array.array("h")
    samples.frombytes(proc.stdout[:len(proc.stdout) // 2 * 2])
    return onsets([x / 32768 for x in samples], rate)


def add_audio_markers(a):
    if a["kind"] == "beats":
        times = _beats(a["path"])
        name, color = "beat", a.get("color") or "Cyan"
    else:
        args = {"path": a["path"]}
        args.update({k: a[k] for k in ("noise_db", "min_duration") if k in a})
        found = M.run_media_tool("detect_silence", args)["silence_intervals"]
        times = [x["start"] for x in found]
        name, color = "silence", a.get("color") or "Yellow"
    limit = a.get("max_markers") or 200
    warnings = [f"found {len(times)}, kept the first {limit}"] if len(times) > limit else []
    times = times[:limit]
    if a.get("dry_run"):
        return _op("planned", times=times, warnings=warnings)
    fps = float(_b("get_state")["timeline_fps"])
    offset, added = a.get("timeline_offset_frames") or 0, 0
    for t in times:
        added += bool(_b("add_marker", frame=offset + round(t * fps), color=color, name=name).get("ok"))
    return _op("success" if added == len(times) else "partial", added == len(times), {"markers_added": added},
               warnings, times=times)


def _segments(a):
    if a.get("segments"):
        return a["segments"]
    if not a.get("path"):
        raise ValueError("provide segments, or a path to transcribe")
    args = {"path": a["path"], "model": a.get("model") or "base"}
    if a.get("language"):
        args["language"] = a["language"]
    return M.run_media_tool("transcribe_audio", args)["segments"]


def add_captions(a):
    segments = _segments(a)
    state = _b("get_state")
    fps = float(state["timeline_fps"])
    base = a["record_frame"] if a.get("record_frame") is not None else state["timeline_start_frame"]
    lines = [{"text": s["text"].strip(), "record_frame": base + round(s["start"] * fps),
              "frames": max(1, round((s["end"] - s["start"]) * fps))} for s in segments if s["text"].strip()]
    if a.get("dry_run") or not lines:
        return _op("planned", captions=lines)
    args = {k: a[k] for k in ("font", "size", "y", "lock_video_tracks") if k in a}
    report = _b("add_text_plus", items=lines, **args)
    made = _list(report.get("items"))
    return _op("success" if len(made) == len(lines) else "partial", len(made) == len(lines),
               {"captions_added": len(made)}, items=made)


def _stamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(segments):
    rows = [s for s in segments if s["text"].strip()]
    return "".join(f"{i}\n{_stamp(s['start'])} --> {_stamp(s['end'])}\n{s['text'].strip()}\n\n"
                   for i, s in enumerate(rows, 1))


def export_srt(a):
    output = a.get("output") or os.path.join(M.DIR, f"captions_{time.strftime('%Y%m%d-%H%M%S')}.srt")
    if os.path.exists(output):
        return _blocked(f"refusing to overwrite {output}")
    with open(output, "w", encoding="utf-8") as f:
        f.write(to_srt(_segments(a)))
    return _op("success", True, {"file_written": output}, output=output)


def audit_log(a):
    try:
        with open(AUDIT, encoding="utf-8") as f:
            rows = f.readlines()
    except OSError:
        return {"entries": []}
    return {"entries": [json.loads(x) for x in rows[-(a.get("limit") or 50):]]}


def doctor(a=None):
    try:
        import importlib.util
        whisper = importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        whisper = False
    report = {"python": os.sys.version.split()[0], "ffmpeg": bool(shutil.which("ffmpeg") or os.environ.get("FFMPEG")),
              "ffprobe": bool(shutil.which("ffprobe") or os.environ.get("FFPROBE")), "faster_whisper": whisper,
              "bridge_folder": M.DIR}
    try:
        res = M.call_bridge("get_state", {}, timeout=6, ack_timeout=4)
        report["bridge"] = "connected" if res.get("ok") else res.get("error")
        report["resolve"] = res.get("result")
    except Exception as exc:
        report["bridge"] = f"not running: start it from the Resolve Console ({type(exc).__name__})"
    return report


HANDLERS = {"doctor": doctor, "timeline_report": lambda a: analyze(_items()), "snapshot_timeline": snapshot_timeline,
            "move_item": move_item, "trim_item": trim_item, "add_audio_markers": add_audio_markers,
            "add_captions": add_captions, "export_srt": export_srt, "audit_log": audit_log}
NEEDS_CONFIRM = {"move_item", "trim_item"}
READ_ONLY = {"doctor", "timeline_report", "audit_log"}


def run(name, args):
    if name in NEEDS_CONFIRM and args.get("confirm") is not True:
        return _blocked("this replaces a clip on the timeline: ask the user, then call again with confirm=true")
    result = HANDLERS[name](args)
    if name not in READ_ONLY and not args.get("dry_run"):
        log_audit("edit__" + name, args, result.get("_operation", {}).get("status"))
    return result
