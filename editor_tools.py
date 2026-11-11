"""High-level edit__ tools built on the Lua bridge and FFmpeg.

Rules every mutating tool follows:
- Without confirm=true it only returns a preview of what it would do.
- It checks first, acts, then reads the timeline back. Results carry an `_operation` block
  (status, verification, changes, warnings), an idea borrowed from the upstream Resolve MCP.
- Clips are never deleted before their replacement exists with the grade and settings copied onto it.
Responses are compact by default; pass detail/limit for more.
"""
import array
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import time
from fractions import Fraction

import mcp_server as M

S, I, B, N = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
IDS = {"type": "array", "items": S}
WHEN = {"type": ["integer", "string"], "description": "Absolute timeline frame or timecode like 01:00:12:05"}
CONFIRM = {"type": "boolean", "description": "Omit for a preview. Set true only after the user approves."}
SEGMENTS = {"type": "array", "items": {"type": "object", "properties": {"start": N, "end": N, "text": S},
                                       "required": ["start", "end", "text"]},
            "description": "Caption lines in seconds. Omit to transcribe `path` instead."}
AUDIT = os.path.join(M.DIR, "audit.jsonl")
CAPTION_BIN = "Captions (Resolve Creative Editor)"
MAX_CARD_FRAMES = 150  # Text+ is inserted at Resolve's standard 5 s length and can't be lengthened by script


def T(name, desc, props=None, req=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(req)}}


TOOLS = [
    T("doctor", "Check Python, FFmpeg, transcription, the upstream engine and the live Resolve bridge."),
    T("timeline_summary", "Compact health check of the current timeline: per-track counts, gaps, overlaps, "
      "transitions and reused source clips, with timecodes. Run before editing. limit caps listed issues.",
      {"limit": I}),
    T("inspect_range", "Items between two points (frames or timecodes). details=true adds grade, transform, "
      "fades, speed and markers per item.", {"start": WHEN, "end": WHEN, "details": B}, ["start", "end"]),
    T("find_media", "Search the media pool by name. Returns id, name, frames, fps.", {"query": S, "limit": I}),
    T("snapshot_timeline", "Duplicate the current timeline as a timestamped backup and stay on the original.",
      {"label": S}),
    T("place_clips", "Put media-pool clips on the timeline at exact frames. Source range is [start_frame, end_frame) "
      "in the clip's own frames. Refuses anything that would overwrite existing items.",
      {"clips": {"type": "array", "items": {"type": "object", "properties": {
          "clip": S, "start_frame": I, "end_frame": I, "record": WHEN, "track_index": I,
          "media_type": {"type": "integer", "enum": [1, 2], "description": "1 video only, 2 audio only"}},
          "required": ["clip", "record"]}}, "confirm": CONFIRM}, ["clips"]),
    T("move_item", "Move a clip (and its linked audio/video) to a new frame or track. Builds the replacement first and "
      "copies grade, transform, audio levels, fades, colour, flags and markers onto it. Keyframes are not copied.",
      {"item_id": S, "record": WHEN, "track_index": I, "include_linked": B, "confirm": CONFIRM},
      ["item_id", "record"]),
    T("trim_item", "Trim a clip (and its linked audio/video). Positive frames shorten that end, negative extend it into "
      "spare source media. Same settings-preserving method as move_item.",
      {"item_id": S, "head_frames": I, "tail_frames": I, "include_linked": B, "confirm": CONFIRM}, ["item_id"]),
    T("set_enabled", "Disable or enable clips: a reversible alternative to deleting them.",
      {"item_ids": IDS, "enabled": B}, ["item_ids", "enabled"]),
    T("set_fades", "Set video or audio fade-in/out lengths in frames on clips, with read-back.",
      {"item_ids": IDS, "fade_in": I, "fade_out": I}, ["item_ids"]),
    T("add_transition", "Add a native transition at the start or end of a clip (Resolve 21.1+). Needs spare source "
      "frames on both sides of the cut.",
      {"item_id": S, "type": S, "position": {"type": "string", "enum": ["start", "end"]},
       "alignment": {"type": "string", "enum": ["left", "center", "right"]}, "duration": I,
       "category": {"type": "string", "enum": ["simple", "fusion", "ofx", "audio"]}}, ["item_id"]),
    T("set_audio", "Set clip volume (dB) and/or pan (-100..100) on audio items, with read-back.",
      {"item_ids": IDS, "volume_db": N, "pan": N}, ["item_ids"]),
    T("normalize_audio", "Normalize audio clips with Resolve's own loudness modes (e.g. 'EBU R128', 'YouTube').",
      {"item_ids": IDS, "mode": S, "target_level": N, "target_loudness": N,
       "independent": {"type": "boolean", "description": "Level each clip separately instead of keeping balance"},
       "confirm": CONFIRM}, ["item_ids"]),
    T("set_speed", "Change clip speed (percentage, 0 = freeze). Doesn't ripple the timeline.",
      {"item_id": S, "percentage": N, "confirm": CONFIRM}, ["item_id", "percentage"]),
    T("copy_grade", "Copy one clip's grade onto other clips.", {"from_id": S, "to_ids": IDS, "confirm": CONFIRM},
      ["from_id", "to_ids"]),
    T("add_markers", "Add several timeline markers at once.",
      {"markers": {"type": "array", "items": {"type": "object", "properties": {
          "at": WHEN, "name": S, "note": S, "color": S, "duration": I}, "required": ["at"]}}}, ["markers"]),
    T("add_audio_markers", "Mark silences or musical onsets found in a file. dry_run only lists them. Measurements, "
      "not edit decisions. timeline_offset_frames is where the file starts relative to the timeline start.",
      {"path": S, "kind": {"type": "string", "enum": ["silence", "beats"]}, "timeline_offset_frames": I,
       "color": S, "noise_db": N, "min_duration": N, "max_markers": I, "dry_run": B}, ["path", "kind"]),
    T("add_captions", "Editable Text+ captions at exact times and lengths, from segments or by transcribing `path`. "
      "record is where the audio starts (default timeline start). Each caption is a small timeline in a bin, "
      "placed like a clip; long lines are split. dry_run only lists them.",
      {"path": S, "segments": SEGMENTS, "record": WHEN, "track_index": I, "max_chars": I, "model": S, "language": S,
       "font": S, "size": N, "y": N, "dry_run": B, "confirm": CONFIRM}),
    T("export_srt", "Write an .srt subtitle file from segments or by transcribing `path`. Import it in Resolve with "
      "File > Import > Subtitle (importing subtitles by script can crash Resolve 21).",
      {"path": S, "segments": SEGMENTS, "output": S, "model": S, "language": S, "max_chars": I}),
    T("export_timeline", "Export the current timeline as OTIO, FCPXML, EDL, AAF or DRT for review or hand-off.",
      {"format": {"type": "string", "enum": ["OTIO", "FCPXML", "EDL", "AAF", "DRT"]}, "output": S}, ["format"]),
    T("qc_file", "Quality check a rendered file: loudness (LUFS, true peak), black frames, frozen frames, silence.",
      {"path": S}, ["path"]),
    T("batch", "Run several edit__ tools in order with one backup, stopping at the first failure. Each operation is "
      "{tool, args}. confirm applies to all of them.",
      {"operations": {"type": "array", "items": {"type": "object", "properties": {"tool": S, "args": {"type": "object"}},
                                                  "required": ["tool"]}}, "confirm": CONFIRM}, ["operations"]),
    T("audit_log", "Recent changes made through this server.", {"limit": I}),
]
NAMES = {t["name"] for t in TOOLS}


# ---------- frame rates and timecode ----------
NTSC = {23.976: Fraction(24000, 1001), 29.97: Fraction(30000, 1001), 47.952: Fraction(48000, 1001),
        59.94: Fraction(60000, 1001), 119.88: Fraction(120000, 1001)}


def parse_fps(value):
    """Resolve reports rates like 30, '29.97' or '23.976'. Returns an exact Fraction."""
    match = re.search(r"\d+(?:\.\d+)?", str(value if value is not None else ""))
    if not match:
        raise ValueError(f"unreadable frame rate: {value!r}")
    rate = float(match.group())
    for nominal, exact in NTSC.items():
        if abs(rate - nominal) < 0.01:
            return exact
    return Fraction(rate).limit_denominator(1001)


def frame_to_tc(frame, fps, drop=False):
    base = round(float(fps))
    frame = int(frame)
    if drop:  # SMPTE drop frame: skip 2 (or 4) frame numbers each minute except every tenth
        skip = round(base / 15)
        per_ten, per_minute = base * 600 - skip * 9, base * 60 - skip
        tens, rest = divmod(frame, per_ten)
        frame += skip * 9 * tens + (skip * ((rest - skip) // per_minute) if rest > skip else 0)
    sep = ";" if drop else ":"
    return (f"{frame // (base * 3600):02d}:{frame // (base * 60) % 60:02d}:{frame // base % 60:02d}"
            f"{sep}{frame % base:02d}")


def tc_to_frame(tc, fps, drop=False):
    parts = [int(x) for x in re.split(r"[:;.]", tc.strip())]
    if len(parts) != 4:
        raise ValueError(f"timecode must look like 01:00:12:05, got {tc!r}")
    h, m, s, f = parts
    base = round(float(fps))
    frame = ((h * 60 + m) * 60 + s) * base + f
    if drop:
        minutes = h * 60 + m
        frame -= round(base / 15) * (minutes - minutes // 10)
    return frame


def seconds_to_frames(seconds, fps):
    return round(Fraction(str(seconds)) * fps)


# ---------- bridge access, cache, audit ----------
_cache = {}


def invalidate():
    _cache.clear()


def _b(op, **args):
    res = M.call_bridge(op, args)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "bridge error"))
    return res.get("result")


def _cached(key, fetch, ttl=3.0):
    """Short-lived cache so one tool call (or a quick sequence) doesn't re-read the timeline. Writes invalidate it."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def _list(value):
    return value if isinstance(value, list) else []


def _state():
    return _cached("state", lambda: _b("get_state"))


def _clock():
    state = _state()
    if not state.get("timeline"):
        raise RuntimeError("no current timeline")
    return parse_fps(state["timeline_fps"]), str(state.get("timeline_drop_frame")) in ("1", "true", "True"), state


def _items():
    return _cached("items", lambda: _list(_b("list_timeline_items", track_type="video")) +
                   _list(_b("list_timeline_items", track_type="audio")))


def _media():
    return _cached("media", lambda: _list(_b("list_media")), ttl=10.0)


def _frame(value):
    """Absolute timeline frame from an int or a timecode string."""
    if isinstance(value, bool) or value is None:
        raise ValueError("expected a frame number or timecode")
    if isinstance(value, (int, float)):
        return int(value)
    fps, drop, _ = _clock()
    return tc_to_frame(str(value), fps, drop)


def _tc(frame):
    fps, drop, _ = _clock()
    return frame_to_tc(frame, fps, drop)


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


def _preview(plan, warnings=()):
    return _op("preview", plan=plan, warnings=warnings, next="Show this to the user; call again with confirm=true.")


# ---------- read-only inspection ----------
def analyze(items, limit=10, tc=str):
    """Pure timeline analysis: gaps, overlaps, transitions, reused source clips."""
    tracks, gaps, overlaps, used, transitions = {}, [], [], {}, 0
    for it in items:
        if it.get("type") == "transition":
            transitions += 1
            continue
        tracks.setdefault((it["track_type"], it["track"]), []).append(it)
        if it["track_type"] == "video" and it.get("type") != "generator":
            used.setdefault(it["name"], []).append(it["start"])
    for (kind, track), row in sorted(tracks.items()):
        row.sort(key=lambda x: x["start"])
        for a, b in zip(row, row[1:]):
            where = f"{'V' if kind == 'video' else 'A'}{track}"
            if b["start"] > a["end"]:
                gaps.append({"track": where, "at": tc(a["end"]), "frames": b["start"] - a["end"]})
            elif b["start"] < a["end"]:
                overlaps.append({"track": where, "at": tc(b["start"]), "frames": a["end"] - b["start"],
                                 "items": [a["id"], b["id"]]})
    reused = {n: [tc(s) for s in starts] for n, starts in sorted(used.items()) if len(starts) > 1}
    return {"items": len(items) - transitions, "transitions": transitions,
            "tracks": {f"{'V' if k == 'video' else 'A'}{t}": len(r) for (k, t), r in sorted(tracks.items())},
            "gaps": {"count": len(gaps), "first": gaps[:limit]},
            "overlaps": {"count": len(overlaps), "first": overlaps[:limit],
                         "note": "Same-track audio overlaps are usually crossfades." if overlaps else None},
            "reused_clips": reused}


def timeline_summary(a):
    _, _, state = _clock()
    report = analyze(_items(), a.get("limit") or 10, _tc)
    report.update({"timeline": state["timeline"], "fps": state["timeline_fps"],
                   "range": [_tc(state["timeline_start_frame"]), _tc(state["timeline_end_frame"])]})
    return report


DEFAULTS = {"Pan": 0, "Tilt": 0, "ZoomX": 1, "ZoomY": 1, "RotationAngle": 0, "Opacity": 100, "CropLeft": 0,
            "CropRight": 0, "CropTop": 0, "CropBottom": 0, "FlipX": False, "FlipY": False, "CompositeMode": 0,
            "AudioVolume": 0, "AudioPan": 0, "AudioPitchSemiTones": 0}


def inspect_range(a):
    start, end = _frame(a["start"]), _frame(a["end"])
    hits = [x for x in _items() if x["start"] < end and start < x["end"]]
    rows = [{"id": x["id"], "name": x["name"], "track": f"{x['track_type'][0].upper()}{x['track']}",
             "type": x.get("type"), "in": _tc(x["start"]), "out": _tc(x["end"]), "frames": x["duration"],
             "source_start": x.get("source_start")} for x in sorted(hits, key=lambda x: (x["start"], x["track_type"]))]
    if a.get("details") and rows:
        details = {d["id"]: d for d in _list(_b("item_details", item_ids=[r["id"] for r in rows if r["type"] != "transition"]))}
        for r in rows:
            d = details.get(r["id"])
            if d:
                props = d.get("properties") or {}
                r.update({"speed": (d.get("speed") or {}).get("Percentage"), "fades": d.get("fades"),
                          "enabled": d.get("enabled"), "color": d.get("color") or None, "markers": len(d.get("markers") or {}),
                          "linked": d.get("linked"), "fusion_comps": d.get("fusion_comps"),
                          "changed_settings": {k: v for k, v in props.items()
                                               if k in DEFAULTS and v != DEFAULTS[k]}})
    return {"range": [_tc(start), _tc(end)], "count": len(rows), "items": rows}


def find_media(a):
    query, limit = (a.get("query") or "").lower(), a.get("limit") or 20
    hits = [m for m in _media() if query in (m.get("name") or "").lower()]
    return {"count": len(hits), "media": [{k: m.get(k) for k in ("id", "name", "frames", "fps", "type")}
                                          for m in hits[:limit]]}


def snapshot_timeline(a):
    current = _state()["timeline"]
    if not current:
        return _blocked("no current timeline")
    name = f"{current} {a.get('label') or 'backup'} {time.strftime('%Y%m%d-%H%M%S')}"
    _b("duplicate_timeline", name=name)  # Resolve makes the copy current
    _b("set_current_timeline", name=current)  # the bridge retries until Resolve stays on it
    invalidate()
    ok = name in _list(_b("list_timelines")) and _b("get_state")["timeline"] == current
    if not ok:
        raise RuntimeError(f"backup made, but Resolve did not return to {current}; nothing was edited")
    return _op("success" if ok else "partial", ok, {"created_timeline": name}, snapshot=name)


# ---------- placing clips ----------
def _clip(ref):
    hits = [m for m in _media() if m.get("id") == ref] or [m for m in _media() if m.get("name") == ref]
    if len(hits) != 1:
        raise ValueError(f"need exactly one media-pool clip matching {ref!r}, found {len(hits)}")
    return hits[0]


def _collides(kind, track, start, end, ignore=()):
    for x in _items():
        if (x["track_type"] == kind and x["track"] == track and x["id"] not in ignore
                and x.get("type") != "transition" and x["start"] < end and start < x["end"]):
            return x
    return None


def place_clips(a):
    plan, warnings = [], []
    for spec in a["clips"]:
        clip = _clip(spec["clip"])
        first = spec.get("start_frame") or 0
        last = spec.get("end_frame") or int(float(clip.get("frames") or 0))
        record, track = _frame(spec["record"]), spec.get("track_index") or 1
        if last <= first:
            return _blocked(f"{clip['name']}: end_frame must be greater than start_frame")
        kinds = {1: ["video"], 2: ["audio"]}.get(spec.get("media_type"), ["video", "audio"])
        for kind in kinds:
            hit = _collides(kind, track, record, record + last - first)
            if hit:
                return _blocked(f"{clip['name']} would overwrite {hit['name']} on {kind[0].upper()}{track} at {_tc(hit['start'])}")
        plan.append({"clip": clip["id"], "name": clip["name"], "start_frame": first, "end_frame": last,
                     "record_frame": record, "track_index": track, "media_type": spec.get("media_type"),
                     "at": _tc(record), "frames": last - first})
    if not a.get("confirm"):
        return _preview(plan, warnings)
    _b("append_clips", clips=[{k: p[k] for k in ("clip", "start_frame", "end_frame", "record_frame", "track_index",
                                                  "media_type") if p[k] is not None} for p in plan])
    invalidate()
    landed = sum(1 for p in plan if any(x["start"] == p["record_frame"] and x["track"] == p["track_index"]
                                        and x["name"] == p["name"] for x in _items()))
    return _op("success" if landed == len(plan) else "partial", landed == len(plan), {"clips_placed": landed})


# ---------- settings-preserving replace (move and trim) ----------
REFUSE_REASONS = [
    (lambda d: d.get("fusion_comps"), "it has a Fusion composition, which can't be copied"),
    (lambda d: d.get("takes"), "it has takes, which can't be copied"),
    (lambda d: (d.get("speed") or {}).get("Percentage", 100) != 100, "it has a speed change; set speed to 100% first"),
    (lambda d: not d.get("media_id"), "it isn't a media-pool clip (title, generator or compound clip)"),
]


def _plan_replace(item_id, include_linked, new_range):
    """new_range(item) -> (first, last, record, track). Returns (plan, warnings) or a reason string."""
    items = {x["id"]: x for x in _items()}
    if item_id not in items:
        return f"timeline item not found: {item_id}"
    root = _list(_b("item_details", item_ids=[item_id]))[0]
    ids = [item_id] + [i for i in (root.get("linked") or []) if include_linked and i in items]
    details = {d["id"]: d for d in _list(_b("item_details", item_ids=ids))}
    fps, _, _ = _clock()
    plan = []
    for i in ids:
        item, d = items[i], details[i]
        for test, why in REFUSE_REASONS:
            if test(d):
                return f"{item['name']} on {item['track_type']} {item['track']}: {why}"
        media = next((m for m in _media() if m.get("id") == d["media_id"]), None)
        if not media or abs(float(parse_fps(media.get("fps"))) - float(fps)) > 0.001:
            return f"{item['name']}: clip frame rate differs from the timeline, so source frames would not line up"
        first, last, record, track = new_range(item)
        frames = int(float(media.get("frames") or 0))
        if first < 0 or last <= first or (frames and last > frames):
            return f"{item['name']}: source range {first}-{last} is outside the clip (0-{frames})"
        hit = _collides(item["track_type"], track, record, record + last - first, ignore=ids)
        if hit:
            return f"{item['name']} would overwrite {hit['name']} at {_tc(hit['start'])}"
        for x in items.values():
            if (x.get("type") == "transition" and x["track_type"] == item["track_type"] and x["track"] == item["track"]
                    and x["start"] <= item["end"] and item["start"] <= x["end"]):
                return f"{item['name']} has a transition touching it; remove it in Resolve first"
        plan.append({"item": item, "detail": d, "media_id": d["media_id"], "first": first, "last": last,
                     "record": record, "track": track, "shift": item["source_start"] - first})
    return plan


def _append_one(media_id, kind, first, last, record, track):
    _b("append_clips", clips=[{"clip": media_id, "start_frame": first, "end_frame": last, "record_frame": record,
                               "track_index": track, "media_type": 1 if kind == "video" else 2}])
    invalidate()
    for x in _items():
        if x["track_type"] == kind and x["track"] == track and x["start"] == record and x["duration"] == last - first:
            return x
    raise RuntimeError(f"append to {kind} {track} at {record} did not land")


def _matches(old, new):
    keys = set(old.get("properties") or {}) & set(new.get("properties") or {})
    diff = [k for k in keys if old["properties"][k] != new["properties"][k]]
    for k in ("fades", "color", "enabled"):
        if old.get(k) != new.get(k):
            diff.append(k)
    return diff


def _replace(plan, backup=True):
    """Build replacements on staging tracks, copy settings, then swap. Original survives any failure before the swap."""
    snapshot = snapshot_timeline({"label": "before edit"}).get("snapshot") if backup else None
    staging, staged, done = {}, [], []

    def stage_track(kind):
        if kind not in staging:
            staging[kind] = _b("add_track", track_type=kind)["count"]
        return staging[kind]

    def cleanup():
        if staged:
            _b("delete_items", item_ids=[s["id"] for s in staged])
        for kind, track in staging.items():
            try:
                _b("delete_empty_track", track_type=kind, track=track)
            except RuntimeError:
                pass
        invalidate()

    try:
        for p in plan:
            kind = p["item"]["track_type"]
            s = _append_one(p["media_id"], kind, p["first"], p["last"], p["record"], stage_track(kind))
            staged.append(s)
            report = _b("copy_settings", from_id=p["item"]["id"], to_id=s["id"], kind=kind, marker_shift=p["shift"])
            if report["failed"]:
                raise RuntimeError(f"could not copy {', '.join(report['failed'])} to the replacement")
            p["staged"] = s
    except Exception as exc:
        cleanup()
        return _op("failed", reason=f"nothing changed: {exc}", snapshot=snapshot)

    _b("delete_items", item_ids=[p["item"]["id"] for p in plan])
    invalidate()
    try:
        for p in plan:
            kind = p["item"]["track_type"]
            n = _append_one(p["media_id"], kind, p["first"], p["last"], p["record"], p["track"])
            _b("copy_settings", from_id=p["staged"]["id"], to_id=n["id"], kind=kind, marker_shift=0)
            done.append((p, n))
    except Exception as exc:
        for p, n in done:
            _b("delete_items", item_ids=[n["id"]])
        restored = []
        for p in plan:
            item = p["item"]
            r = _append_one(p["media_id"], item["track_type"], item["source_start"],
                            item["source_start"] + item["duration"], item["start"], item["track"])
            _b("copy_settings", from_id=p["staged"]["id"], to_id=r["id"], kind=item["track_type"],
               marker_shift=p["first"] - item["source_start"])
            restored.append(r["id"])
        if len(restored) > 1:
            _b("link_items", item_ids=restored)
        cleanup()
        return _op("failed", reason=f"swap failed ({exc}); the original was rebuilt with its settings",
                   snapshot=snapshot, warnings=["keyframes on the original are lost"])

    if len(done) > 1:
        _b("link_items", item_ids=[n["id"] for _, n in done])
    after = {d["id"]: d for d in _list(_b("item_details", item_ids=[n["id"] for _, n in done]))}
    problems = []
    for p, n in done:
        diff = _matches(p["detail"], after[n["id"]])
        if diff:
            problems.append(f"{p['item']['name']}: {', '.join(diff)} differ")
    cleanup()
    changes = [{"item": p["item"]["name"], "track": f"{p['item']['track_type'][0].upper()}{p['track']}",
                "from": [_tc(p["item"]["start"]), p["item"]["duration"]], "to": [_tc(p["record"]), p["last"] - p["first"]],
                "new_id": n["id"]} for p, n in done]
    return _op("success" if not problems else "partial", not problems, {"replaced": changes},
               ["keyframes are not copied"] + problems, snapshot=snapshot)


def _replace_tool(a, new_range, verb):
    plan = _plan_replace(a["item_id"], a.get("include_linked", True), new_range)
    if isinstance(plan, str):
        return _blocked(plan)
    rows = [{"item": p["item"]["name"], "track": f"{p['item']['track_type'][0].upper()}{p['track']}",
             "from": [_tc(p["item"]["start"]), p["item"]["duration"]], "to": [_tc(p["record"]), p["last"] - p["first"]]}
            for p in plan]
    if not a.get("confirm"):
        return _preview({verb: rows, "method": "replacement built and settings copied before the original is removed"},
                        ["keyframes are not copied", "a backup timeline is made first"])
    return _replace(plan, a.get("backup", True))


def move_item(a):
    target = _frame(a["record"])
    root = next((x for x in _items() if x["id"] == a["item_id"]), None)
    if not root:
        return _blocked(f"timeline item not found: {a['item_id']}")
    shift = target - root["start"]
    return _replace_tool(a, lambda it: (it["source_start"], it["source_start"] + it["duration"], it["start"] + shift,
                                        a.get("track_index") or it["track"] if it["id"] == a["item_id"] else it["track"]),
                         "move")


def trim_item(a):
    head, tail = a.get("head_frames") or 0, a.get("tail_frames") or 0
    if not head and not tail:
        return _blocked("give head_frames and/or tail_frames")
    return _replace_tool(a, lambda it: (it["source_start"] + head, it["source_start"] + it["duration"] - tail,
                                        it["start"] + head, it["track"]), "trim")


# ---------- clip settings ----------
def set_enabled(a):
    r = _b("set_enabled", item_ids=a["item_ids"], enabled=bool(a["enabled"]))
    got = [d.get("enabled") for d in _list(_b("item_details", item_ids=a["item_ids"]))]
    ok = all(e == bool(a["enabled"]) for e in got)
    return _op("success" if ok else "partial", ok, {"items_changed": r.get("changed")})


def set_fades(a):
    results = []
    for i in a["item_ids"]:
        r = _b("set_fades", item_id=i, fade_in=a.get("fade_in") or 0, fade_out=a.get("fade_out") or 0)
        fades = r.get("fades") or {}
        results.append(int(fades.get("FadeIn", -1)) == (a.get("fade_in") or 0) and
                       int(fades.get("FadeOut", -1)) == (a.get("fade_out") or 0))
    return _op("success" if all(results) else "partial", all(results), {"items_changed": sum(results)})


def add_transition(a):
    options = {"type": a.get("type") or "Cross Dissolve", "category": a.get("category") or "simple",
               "position": a.get("position") or "end", "alignment": a.get("alignment") or "center"}
    if a.get("duration"):
        options["duration"] = a["duration"]
    r = _b("add_transition", item_id=a["item_id"], options=options)
    invalidate()
    ok = not a.get("duration") or r.get("duration") == a["duration"]
    return _op("success" if ok else "partial", ok, {"transition": r.get("name"), "at": _tc(r["start"]),
                                                    "frames": r.get("duration")}, transition_id=r.get("id"))


def set_audio(a):
    props = {}
    if a.get("volume_db") is not None:
        props["AudioVolume"] = float(a["volume_db"])
    if a.get("pan") is not None:
        props["AudioPan"] = float(a["pan"])
    if not props:
        return _blocked("give volume_db and/or pan")
    ok_count = 0
    for i in a["item_ids"]:
        _b("set_item_properties", item_id=i, properties=props)
    for d in _list(_b("item_details", item_ids=a["item_ids"])):
        got = d.get("properties") or {}
        ok_count += all(abs(float(got.get(k, 1e9)) - v) < 0.01 for k, v in props.items())
    ok = ok_count == len(a["item_ids"])
    return _op("success" if ok else "partial", ok, {"items_changed": ok_count},
               [] if ok else ["some items are not audio, or Resolve clamped the value"])


def normalize_audio(a):
    modes = [v for k, v in (_b("audio_modes") or {}).items() if k != "__flags"]
    mode = a.get("mode") or "EBU R128"
    if mode not in modes:
        return _blocked(f"unknown mode {mode!r}; choose from {modes}")
    options = {"normalizationMode": mode,
               "setLevelMode": "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT" if a.get("independent")
               else "NORMALIZE_AUDIO_SET_LEVEL_RELATIVE"}
    if a.get("target_level") is not None:
        options["targetLevel"] = a["target_level"]
    if a.get("target_loudness") is not None:
        options["targetLoudness"] = a["target_loudness"]
    if not a.get("confirm"):
        return _preview({"items": len(a["item_ids"]), "options": options}, ["changes clip gain; Ctrl+Z in Resolve undoes it"])
    before = {d["id"]: (d.get("properties") or {}).get("AudioVolume") for d in _list(_b("item_details", item_ids=a["item_ids"]))}
    ok = _b("normalize_audio", item_ids=a["item_ids"], options=options).get("ok")
    after = {d["id"]: (d.get("properties") or {}).get("AudioVolume") for d in _list(_b("item_details", item_ids=a["item_ids"]))}
    return _op("success" if ok else "failed", None, {"gain_db": {i: [before.get(i), after.get(i)] for i in after}},
               ["clip-volume read-back may not reflect normalization gain; listen to confirm"])


def set_speed(a):
    if not a.get("confirm"):
        return _preview({"item": a["item_id"], "percentage": a["percentage"]},
                        ["the clip keeps its timeline length, so it shows more or less of the source"])
    r = _b("set_speed", item_id=a["item_id"], percentage=float(a["percentage"]))
    invalidate()
    got = (r.get("speed") or {}).get("Percentage")
    ok = got is not None and abs(float(got) - float(a["percentage"])) < 0.01
    return _op("success" if ok else "failed", ok, {"speed": got})


def copy_grade(a):
    if not a.get("confirm"):
        return _preview({"from": a["from_id"], "to": a["to_ids"]}, ["replaces the grade on the target clips"])
    ok = _b("copy_grade", from_id=a["from_id"], to_ids=a["to_ids"]).get("ok")
    return _op("success" if ok else "failed", None, {"clips_graded": len(a["to_ids"]) if ok else 0},
               ["grades can't be read back by script; check the Color page"])


def add_markers(a):
    _, _, state = _clock()
    start = state["timeline_start_frame"]
    markers = [{"frame": _frame(m["at"]) - start, "name": m.get("name") or "", "note": m.get("note") or "",
                "color": m.get("color") or "Blue", "duration": m.get("duration") or 1} for m in a["markers"]]
    added = _b("add_markers", markers=markers).get("added", 0)
    return _op("success" if added == len(markers) else "partial", added == len(markers), {"markers_added": added},
               [] if added == len(markers) else ["a marker already exists at some of those frames"])


# ---------- audio analysis ----------
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
        found = [{"start": t, "end": t} for t in _beats(a["path"])]
        name, color = "beat", a.get("color") or "Cyan"
    else:
        args = {"path": a["path"]}
        args.update({k: a[k] for k in ("noise_db", "min_duration") if k in a})
        result = M.run_media_tool("detect_silence", args)
        length = float(M.run_media_tool("probe_media", {"path": a["path"]}).get("duration") or 0)
        found = [{"start": x["start"], "end": x["end"] if x["end"] is not None else length}
                 for x in result["silence_intervals"]]
        name, color = "silence", a.get("color") or "Yellow"
    limit = a.get("max_markers") or 200
    warnings = [f"found {len(found)}, kept the first {limit}"] if len(found) > limit else []
    found = found[:limit]
    if a.get("dry_run"):
        return _op("planned", times=[[x["start"], x["end"]] for x in found], warnings=warnings)
    fps, _, _ = _clock()
    offset = a.get("timeline_offset_frames") or 0
    markers = [{"frame": offset + seconds_to_frames(x["start"], fps), "name": name, "color": color,
                "duration": max(1, seconds_to_frames(x["end"] - x["start"], fps))} for x in found]
    added = _b("add_markers", markers=markers).get("added", 0) if markers else 0
    return _op("success" if added == len(markers) else "partial", added == len(markers), {"markers_added": added},
               warnings)


# ---------- captions ----------
def split_segments(segments, max_chars=42):
    """Split long lines at word boundaries, sharing time by length, and stop lines overlapping."""
    out = []
    for s in segments:
        words, text = s["text"].split(), s["text"].strip()
        if not text:
            continue
        lines, line = [], ""
        for w in words:
            if line and len(line) + 1 + len(w) > max_chars:
                lines.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        lines.append(line)
        total, t = sum(len(x) for x in lines), float(s["start"])
        for x in lines:
            end = t + (float(s["end"]) - float(s["start"])) * len(x) / total
            out.append({"start": round(t, 3), "end": round(end, 3), "text": x})
            t = end
    out.sort(key=lambda x: x["start"])
    for a, b in zip(out, out[1:]):
        a["end"] = min(a["end"], b["start"])
    return [x for x in out if x["end"] > x["start"]]


def _segments(a):
    if a.get("segments"):
        return split_segments(a["segments"], a.get("max_chars") or 42)
    if not a.get("path"):
        raise ValueError("provide segments, or a path to transcribe")
    args = {"path": a["path"], "model": a.get("model") or "base"}
    if a.get("language"):
        args["language"] = a["language"]
    return split_segments(M.run_media_tool("transcribe_audio", args)["segments"], a.get("max_chars") or 42)


def caption_cards(segments, fps, base, timeline, stamp=""):
    """Timeline names must be unique in a project, so each run's cards carry a time stamp."""
    cards, timeline = [], f"{timeline} {stamp}".strip()
    for n, s in enumerate(segments, 1):
        start, end = base + seconds_to_frames(s["start"], fps), base + seconds_to_frames(s["end"], fps)
        while end - start > 0:  # a card can be at most 5 s, so longer lines continue on a second card
            frames = min(end - start, MAX_CARD_FRAMES)
            cards.append({"name": f"{timeline} caption {n:04d}{'' if frames == end - start else '+'}"[:120],
                          "text": s["text"], "record_frame": start, "frames": frames})
            start += frames
    return cards


def add_captions(a):
    segments = _segments(a)
    fps, _, state = _clock()
    base = _frame(a["record"]) if a.get("record") is not None else state["timeline_start_frame"]
    cards = caption_cards(segments, fps, base, state["timeline"], time.strftime("%m%d-%H%M%S"))
    preview = [{"at": _tc(c["record_frame"]), "frames": c["frames"], "text": c["text"]} for c in cards]
    if a.get("dry_run") or not a.get("confirm") or not cards:
        return _preview({"captions": preview[: a.get("limit") or 20], "count": len(cards), "bin": CAPTION_BIN},
                        ["each caption is a small timeline in the bin; edit its text there"])
    track = a.get("track_index")
    if not track:
        track = _b("add_track", track_type="video")["count"]
    for c in cards:
        hit = _collides("video", track, c["record_frame"], c["record_frame"] + c["frames"])
        if hit:
            return _blocked(f"V{track} is busy at {_tc(hit['start'])}; leave track_index out to use a new track")
    style = {k: a[k] for k in ("font", "size", "y") if k in a}
    r = _b("add_title_cards", cards=cards, folder=a.get("folder") or CAPTION_BIN, track_index=track, **style)
    invalidate()
    placed = _list(r.get("placed"))
    ok = len(placed) == len(cards) and all(p["duration"] == c["frames"] for p, c in zip(placed, cards))
    return _op("success" if ok else ("partial" if placed else "failed"), ok,
               {"captions_added": len(placed), "track": f"V{track}"}, [r["error"]] if r.get("error") else [])


def _stamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(segments):
    rows = [s for s in segments if s["text"].strip()]
    return "".join(f"{i}\n{_stamp(s['start'])} --> {_stamp(s['end'])}\n{s['text'].strip()}\n\n"
                   for i, s in enumerate(rows, 1))


def _output(a, suffix):
    output = a.get("output") or os.path.join(M.DIR, f"export_{time.strftime('%Y%m%d-%H%M%S')}{suffix}")
    if os.path.exists(output):
        raise FileExistsError(f"refusing to overwrite {output}")
    return output


def export_srt(a):
    output = _output(a, ".srt")
    with open(output, "w", encoding="utf-8") as f:
        f.write(to_srt(_segments(a)))
    return _op("success", True, {"file_written": output}, output=output)


def export_timeline(a):
    suffix = {"OTIO": ".otio", "FCPXML": ".fcpxml", "EDL": ".edl", "AAF": ".aaf", "DRT": ".drt"}[a["format"]]
    output = _output(a, suffix)
    ok = _b("export_timeline", path=output, format=a["format"]).get("ok") and os.path.exists(output)
    return _op("success" if ok else "failed", ok, {"file_written": output if ok else None})


# ---------- final checks ----------
def parse_qc(log):
    def last(pattern):
        hits = re.findall(pattern, log)
        return float(hits[-1]) if hits else None
    black = [[float(a), float(b)] for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    freeze = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    silence = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    return {"integrated_lufs": last(r"I:\s+(-?[\d.]+) LUFS"), "loudness_range_lu": last(r"LRA:\s+([\d.]+) LU"),
            "true_peak_dbfs": last(r"Peak:\s+(-?[\d.]+) dBFS"), "black_segments": black[:20],
            "frozen_at_seconds": freeze[:20], "silence_at_seconds": silence[:20]}


def qc_file(a):
    ffmpeg = M._binary("ffmpeg", "FFMPEG")
    proc = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", M._media_path(a["path"]),
                           "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2",
                           "-af", "ebur128=peak=true,silencedetect=n=-50dB:d=1", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-2000:])
    report = parse_qc(proc.stderr)
    notes = []
    if report["true_peak_dbfs"] is not None and report["true_peak_dbfs"] > -1:
        notes.append("true peak above -1 dBFS may clip after upload encoding")
    if report["integrated_lufs"] is not None and not -16 <= report["integrated_lufs"] <= -12:
        notes.append("YouTube-style targets are around -14 LUFS")
    return _op("success", True, warnings=notes, qc=report)


def audit_log(a):
    try:
        with open(AUDIT, encoding="utf-8") as f:
            rows = f.readlines()
    except OSError:
        return {"entries": []}
    return {"entries": [json.loads(x) for x in rows[-(a.get("limit") or 20):]]}


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


def batch(a):
    ops = a["operations"]
    unknown = [o.get("tool") for o in ops if o.get("tool") not in HANDLERS or o.get("tool") == "batch"]
    if unknown:
        return _blocked(f"unknown or nested tools: {unknown}")
    if not a.get("confirm"):
        return _preview([{"tool": o["tool"], "preview": run(o["tool"], dict(o.get("args") or {}))} for o in ops])
    snapshot = snapshot_timeline({"label": "before batch"}).get("snapshot")
    results = []
    for o in ops:
        args = dict(o.get("args") or {}, confirm=True, backup=False)
        r = run(o["tool"], args)
        status = r.get("_operation", {}).get("status", "success")
        results.append({"tool": o["tool"], "status": status, "reason": r.get("reason")})
        if status in ("failed", "blocked"):
            break
    ok = all(r["status"] in ("success", "planned") for r in results) and len(results) == len(ops)
    return _op("success" if ok else "partial", ok, {"steps_done": len(results)}, results=results, snapshot=snapshot)


HANDLERS = {"doctor": doctor, "timeline_summary": timeline_summary, "inspect_range": inspect_range,
            "find_media": find_media, "snapshot_timeline": snapshot_timeline, "place_clips": place_clips,
            "move_item": move_item, "trim_item": trim_item, "set_enabled": set_enabled, "set_fades": set_fades,
            "add_transition": add_transition, "set_audio": set_audio, "normalize_audio": normalize_audio,
            "set_speed": set_speed, "copy_grade": copy_grade, "add_markers": add_markers,
            "add_audio_markers": add_audio_markers, "add_captions": add_captions, "export_srt": export_srt,
            "export_timeline": export_timeline, "qc_file": qc_file, "batch": batch, "audit_log": audit_log}
READ_ONLY = {"doctor", "timeline_summary", "inspect_range", "find_media", "audit_log", "qc_file"}


def run(name, args):
    expected = None
    if name not in READ_ONLY and args.get("confirm"):
        expected = _b("get_state").get("timeline")  # catch Resolve switching timelines under us mid-edit
    result = HANDLERS[name](args)
    if expected and isinstance(result, dict):
        now = _b("get_state").get("timeline")
        if now != expected:
            _b("set_current_timeline", name=expected)
            result.setdefault("_operation", {}).setdefault("warnings", []).append(
                f"Resolve had switched to {now}; switched back to {expected}")
    status = result.get("_operation", {}).get("status") if isinstance(result, dict) else None
    if name not in READ_ONLY and status not in (None, "preview", "planned", "blocked"):
        invalidate()
        log_audit("edit__" + name, args, status)
    return result
