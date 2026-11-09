"""MCP server (stdio, stdlib only) that forwards tool calls to resolve_bridge.lua running inside Resolve.

Transport: request.lua in %USERPROFILE%\\resolve-claude-bridge; replies come back through Fusion.prefs.
"""
import base64
import json
import os
import re
import sys
import time
import uuid

# Not under AppData: the Claude desktop app is MSIX-packaged and its AppData\Local writes are redirected
# into its private package folder, invisible to Resolve.
DIR = os.environ.get("RESOLVE_BRIDGE_DIR") or os.path.join(os.path.expanduser("~"), "resolve-claude-bridge")
os.makedirs(DIR, exist_ok=True)
NOT_RUNNING = ("Bridge not running. In Resolve open Workspace > Console (Lua tab) and run: "
               'dofile(os.getenv("APPDATA").."\\\\Blackmagic Design\\\\DaVinci Resolve\\\\Support\\\\Fusion\\\\Scripts'
               '\\\\Utility\\\\resolve_bridge.lua")')

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
STRS = {"type": "array", "items": S}
CLIP_REF = "clip id (preferred) or exact name"


def T(name, desc, props=None, req=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(req)}}


TOOLS = [
    T("get_state", "Resolve version, current page/project/timeline, fps, frame range, playhead timecode."),
    T("list_timelines", "Names of all timelines in the current project."),
    T("set_current_timeline", "Switch the active timeline.", {"name": S}, ["name"]),
    T("list_media", "All clips in the media pool (id, name, duration, frames, fps, resolution, path)."),
    T("import_media", "Import files from disk into the media pool.", {"paths": STRS}, ["paths"]),
    T("create_timeline", "Create a timeline, empty or from media-pool clips.",
      {"name": S, "clips": {"type": "array", "items": S, "description": CLIP_REF}}, ["name"]),
    T("append_clips",
      "Append clips to the current timeline. Use start_frame/end_frame (source frames, inclusive) to cut a "
      "sub-range; this is the way to trim/cut. record_frame places it at a timeline position.",
      {"clips": {"type": "array", "items": {"type": "object", "properties": {
          "clip": S, "start_frame": I, "end_frame": I, "track_index": I, "record_frame": I,
          "media_type": {"type": "integer", "description": "1=video only, 2=audio only"}},
          "required": ["clip"]}}}, ["clips"]),
    T("add_track", "Add a track (audio is stereo) to the current timeline.",
      {"track_type": {"type": "string", "enum": ["audio", "video"]}}),
    T("duplicate_timeline", "Duplicate the current timeline under a new name.", {"name": S}, ["name"]),
    T("set_grade", "Primary colour grade (ASC CDL on a node, default 1) for timeline items. slope/offset/power are "
      "'r g b' strings, e.g. slope '1.05 1.0 0.95' warms; saturation e.g. 1.15. No values = reset to neutral.",
      {"item_ids": STRS, "slope": S, "offset": S, "power": S, "saturation": {"type": "number"}, "node": I}, ["item_ids"]),
    T("probe_color", "Report which colour-page calls Resolve exposes for a timeline item (changes nothing).",
      {"item_id": S}, ["item_id"]),
    T("refresh_luts", "Make Resolve rescan its LUT folders."),
    T("set_lut", "Load a .cube LUT onto a node (default 1) of timeline items.",
      {"item_ids": STRS, "path": S, "node": I}, ["item_ids", "path"]),
    T("list_timeline_items", "Items on the current timeline's tracks.",
      {"track_type": {"type": "string", "enum": ["video", "audio", "subtitle"]}}),
    T("set_item_properties",
      "Set transform/composite props on a timeline item (Pan, Tilt, ZoomX, ZoomY, Opacity, RotationAngle, "
      "Crop*, FlipX/Y, ...).", {"item_id": S, "properties": {"type": "object"}}, ["item_id", "properties"]),
    T("delete_items", "Delete timeline items.", {"item_ids": STRS, "ripple": B}, ["item_ids"]),
    T("add_marker", "Add a timeline marker. frame is relative to the timeline start. Colors: Blue, Cyan, Green, "
      "Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream.",
      {"frame": I, "color": S, "name": S, "note": S, "duration": I}, ["frame"]),
    T("list_markers", "All timeline markers keyed by frame."),
    T("delete_marker", "Delete the marker at a frame.", {"frame": I}, ["frame"]),
    T("goto_timecode", "Move the playhead, e.g. 01:00:05:00.", {"timecode": S}, ["timecode"]),
    T("export_frame", "Return the frame at a timecode (or the playhead) as an image so you can SEE the result.",
      {"timecode": S}),
    T("open_page", "Switch Resolve page.",
      {"page": {"type": "string", "enum": ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]}},
      ["page"]),
    T("save_project", "Save the current project."),
    T("list_render_presets", "Available render presets."),
    T("queue_render", "Queue the current timeline for render (does not start it).",
      {"target_dir": S, "name": S, "preset": S}, ["target_dir", "name"]),
    T("start_render", "Start rendering queued jobs (all, or the given job ids).", {"job_ids": STRS}),
    T("render_status", "Render progress and per-job status."),
]


# Resolve's Lua cannot write files, so the bridge replies via Fusion's prefs file (see resolve_bridge.lua).
PREFS = os.environ.get("RESOLVE_FUSION_PREFS") or os.path.join(
    os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Profiles",
    "Default", "Fusion.prefs")
RES_RE = re.compile(r'claude_bridge_res\s*=\s*"([0-9a-f]*):([0-9a-f]*)"')


def read_reply():
    try:
        with open(PREFS, encoding="utf-8", errors="replace") as f:
            m = RES_RE.search(f.read())
        return (m.group(1), m.group(2)) if m else (None, None)
    except OSError:  # being rewritten
        return None, None


def lua_long_string(s):
    n = 0
    while "]" + "=" * n + "]" in s:
        n += 1
    return "[" + "=" * n + "[" + s + "]" + "=" * n + "]"


def call_bridge(op, args, timeout=120, ack_timeout=15):  # Resolve can stall a few seconds (e.g. LUT rescans)
    req_path = os.path.join(DIR, "request.lua")
    rid = uuid.uuid4().hex
    with open(req_path + ".tmp", "w", encoding="utf-8") as f:
        f.write("return " + lua_long_string(json.dumps({"id": rid, "op": op, "args": args}, ensure_ascii=False)))
    os.replace(req_path + ".tmp", req_path)
    try:
        start, acked = time.time(), False
        while time.time() - start < timeout:
            got, payload = read_reply()
            if got == rid:
                if payload:
                    try:
                        return json.loads(bytes.fromhex(payload).decode("utf-8"))
                    except ValueError:  # caught mid-write
                        pass
                acked = True
            if not acked and time.time() - start > ack_timeout:
                raise ConnectionError(NOT_RUNNING)
            time.sleep(0.05)
        raise TimeoutError(f"Resolve did not answer {op} within {timeout}s")
    finally:
        try:
            os.remove(req_path)
        except OSError:
            pass


def run_tool(name, args):
    if name not in {t["name"] for t in TOOLS}:
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    try:
        res = call_bridge(name, args)
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not res.get("ok"):
        return {"content": [{"type": "text", "text": res.get("error", "unknown error")}], "isError": True}
    content = [{"type": "text", "text": json.dumps(res.get("result"), indent=1)}]
    if name == "export_frame":
        try:
            with open(res["result"]["path"], "rb") as f:
                content = [{"type": "image", "data": base64.b64encode(f.read()).decode(), "mimeType": "image/png"}]
        except OSError as e:
            return {"content": [{"type": "text", "text": f"frame export not readable: {e}"}], "isError": True}
    return {"content": content}


def handle(msg):
    m, p = msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "davinci-resolve", "version": "0.2.0"}}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        return run_tool(p.get("name"), p.get("arguments") or {})
    if m == "ping":
        return {}
    raise LookupError(m)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if "id" not in msg:  # notification
            continue
        try:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError:
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
