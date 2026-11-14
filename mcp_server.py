"""MCP server (stdio, stdlib only) that forwards tool calls to resolve_bridge.lua running inside Resolve.

Transport: request.lua in %USERPROFILE%\\resolve-claude-bridge. Replies come back through Fusion.prefs
(Resolve 21.1 and older) or through reply.csv, a metadata export of the bridge's token clip (Resolve 21.2+).
"""
import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import struct
import zlib

# Not under AppData: the Claude desktop app is MSIX-packaged and its AppData\Local writes are redirected
# into its private package folder, invisible to Resolve.
DIR = os.environ.get("RESOLVE_BRIDGE_DIR") or os.path.join(os.path.expanduser("~"), "resolve-claude-bridge")
os.makedirs(DIR, exist_ok=True)
NOT_RUNNING = ("Bridge not running. In Resolve open Workspace > Console (Lua tab) and run "
               '(Resolve 21.2+): fusion:RunScript(os.getenv("USERPROFILE").."\\\\resolve-claude-bridge\\\\resolve_bridge.lua") '
               '(Resolve 21.1 and older): dofile(os.getenv("USERPROFILE").."\\\\resolve-claude-bridge\\\\resolve_bridge.lua")')

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
      "Append clips to the current timeline. Use start_frame/end_frame as a half-open source range [start_frame, end_frame): end_frame is exclusive. Duration is end-start. To cut a "
      "sub-range; this is the way to trim/cut. record_frame places it at a timeline position.",
      {"clips": {"type": "array", "items": {"type": "object", "properties": {
          "clip": S, "start_frame": I, "end_frame": I, "track_index": I, "record_frame": I,
          "media_type": {"type": "integer", "enum": [1, 2], "description": "1=video only, 2=audio only"}},
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
    T("add_text_plus", "Insert editable Text+ titles at timeline frames, set their text and optional style. "
      "lock_video_tracks keeps titles off those tracks while inserting.",
      {"items": {"type": "array", "items": {"type": "object", "properties": {"text": S, "record_frame": I, "frames": I},
                                            "required": ["text", "record_frame"]}},
       "font": S, "style": S, "size": {"type": "number"}, "x": {"type": "number"}, "y": {"type": "number"},
       "lock_video_tracks": {"type": "array", "items": I},
       "inputs": {"type": "object", "description": "other numeric Text+ inputs by id"},
       "list_inputs": B}, ["items"]),
    T("style_text_plus", "Restyle existing Text+ titles (font, style, size, x, y, other numeric inputs by id).",
      {"item_ids": STRS, "font": S, "style": S, "size": {"type": "number"}, "x": {"type": "number"},
       "y": {"type": "number"}, "inputs": {"type": "object"}}, ["item_ids"]),
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
    T("probe_media", "Inspect a media file with ffprobe: duration, codecs, resolution, frame rate, audio streams.",
      {"path": S}, ["path"]),
    T("extract_audio", "Extract the first audio stream to a 48 kHz stereo WAV for auditioning or external analysis.",
      {"path": S, "sample_rate": I}, ["path"]),
    T("detect_silence", "Detect silence intervals in an audio/video file using FFmpeg. Thresholds are configurable.",
      {"path": S, "noise_db": {"type": "number"}, "min_duration": {"type": "number"}}, ["path"]),
    T("transcribe_audio", "Optional local transcription using faster-whisper. Install faster-whisper separately; no model is downloaded automatically.",
      {"path": S, "model": {"type": "string", "enum": ["tiny", "base", "small"]},
       "language": S}, ["path"]),
    T("make_contact_sheet", "Create a visual contact sheet from a video so the editor can review many shots at once.",
      {"path": S, "interval_seconds": {"type": "number"}, "columns": I}, ["path"]),
]


# Resolve's Lua cannot write files, so the bridge replies via Fusion's prefs file (see resolve_bridge.lua).
PREFS = os.environ.get("RESOLVE_FUSION_PREFS") or os.path.join(
    os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Profiles",
    "Default", "Fusion.prefs")
RES_RE = re.compile(r'claude_bridge_res\s*=\s*"([0-9a-f]*):([0-9a-f]*)"')
# Resolve 21.2+ (no SavePrefs): the bridge writes the reply into its token clip's Comments metadata and
# exports it here with MediaPool:ExportMetadata (UTF-16 CSV).
REPLY_CSV = os.path.join(DIR, "reply.csv")
TOKEN_PNG = os.path.join(DIR, "bridge_token.png")
VALUE_RE = re.compile(r"([0-9a-f]*):([0-9a-f]*)")


def _read_prefs_reply():
    try:
        with open(PREFS, encoding="utf-8", errors="replace") as f:
            m = RES_RE.search(f.read())
        return (m.group(1), m.group(2)) if m else (None, None)
    except OSError:  # being rewritten
        return None, None


def _read_csv_reply():
    try:
        with open(REPLY_CSV, encoding="utf-16", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeError, csv.Error):  # missing, or caught mid-write
        return None, None
    for row in rows:
        m = VALUE_RE.fullmatch((row.get("Comments") or "").strip())
        if m:
            return m.group(1), m.group(2)
    return None, None


def read_reply(want=None):
    """Latest reply from either transport; with `want`, only a reply for that request id."""
    for reader in (_read_csv_reply, _read_prefs_reply):
        got, payload = reader()
        if got is not None and (want is None or got == want):
            return got, payload
    return None, None


def ensure_token_png():
    """The 21.2+ bridge imports this 16x16 black PNG as its reply carrier; Resolve's Lua cannot create it."""
    if os.path.isfile(TOKEN_PNG):
        return

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * 16 for _ in range(16))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(TOKEN_PNG + ".tmp", "wb") as f:
        f.write(png)
    os.replace(TOKEN_PNG + ".tmp", TOKEN_PNG)


def lua_long_string(s):
    n = 0
    while "]" + "=" * n + "]" in s:
        n += 1
    return "[" + "=" * n + "[" + s + "]" + "=" * n + "]"


def call_bridge(op, args, timeout=120, ack_timeout=15):  # Resolve can stall a few seconds (e.g. LUT rescans)
    req_path = os.path.join(DIR, "request.lua")
    rid = uuid.uuid4().hex
    ensure_token_png()
    body = lua_long_string(json.dumps({"id": rid, "op": op, "args": args}, ensure_ascii=False))
    # 21.1 and older load this with loadfile and use the returned string; 21.2+ runs it with
    # fusion:RunScript, which cannot return values, so it also hands the JSON over with fusion:SetData.
    with open(req_path + ".tmp", "w", encoding="utf-8") as f:
        f.write("local s = " + body + "\n"
                "if fusion and fusion.SetData then fusion:SetData(\"claude_bridge_req\", s) end\n"
                "return s\n")
    os.replace(req_path + ".tmp", req_path)
    try:
        start, acked = time.time(), False
        while time.time() - start < timeout:
            got, payload = read_reply(rid)
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


def _media_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    path = os.path.abspath(os.path.expanduser(value))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"media file not found: {path}")
    return path


def _binary(name, env_name):
    candidate = os.environ.get(env_name) or shutil.which(name)
    if not candidate:
        raise RuntimeError(f"{name} was not found. Install FFmpeg and ensure {name} is on PATH, or set {env_name}.")
    return candidate


def _output_path(source, suffix):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(source))[0])[:48] or "media"
    return os.path.join(DIR, f"{stem}_{uuid.uuid4().hex[:8]}{suffix}")


def _run_media_command(cmd, timeout=300):
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown FFmpeg error")[-4000:]
        raise RuntimeError(detail)
    return proc


def run_media_tool(name, args):
    source = _media_path(args.get("path"))
    if name == "probe_media":
        ffprobe = _binary("ffprobe", "FFPROBE")
        proc = _run_media_command([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", source], 60)
        info = json.loads(proc.stdout)
        fmt = info.get("format", {})
        streams = []
        for stream in info.get("streams", []):
            streams.append({
                "index": stream.get("index"), "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"), "duration": stream.get("duration"),
                "sample_rate": stream.get("sample_rate"), "channels": stream.get("channels"),
                "width": stream.get("width"), "height": stream.get("height"),
                "frame_rate": stream.get("avg_frame_rate") or stream.get("r_frame_rate"),
                "pixel_format": stream.get("pix_fmt")
            })
        return {"path": source, "duration": fmt.get("duration"), "format": fmt.get("format_name"),
                "bit_rate": fmt.get("bit_rate"), "streams": streams}
    if name == "extract_audio":
        ffmpeg = _binary("ffmpeg", "FFMPEG")
        rate = int(args.get("sample_rate") or 48000)
        if rate not in (16000, 24000, 44100, 48000):
            raise ValueError("sample_rate must be 16000, 24000, 44100 or 48000")
        output = _output_path(source, ".wav")
        _run_media_command([ffmpeg, "-nostdin", "-y", "-i", source, "-map", "0:a:0",
                            "-vn", "-ac", "2", "-ar", str(rate), "-c:a", "pcm_s16le", output], 600)
        return {"source": source, "audio_path": output, "sample_rate": rate, "channels": 2,
                "note": "This is an extracted, playable WAV file; it has not been subjectively auditioned."}
    if name == "detect_silence":
        ffmpeg = _binary("ffmpeg", "FFMPEG")
        noise = float(args.get("noise_db", -35))
        duration = float(args.get("min_duration", 0.35))
        if not -90 <= noise <= 0 or not 0.05 <= duration <= 10:
            raise ValueError("noise_db must be -90..0 and min_duration must be 0.05..10 seconds")
        proc = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", source,
                               "-af", f"silencedetect=noise={noise}dB:d={duration}", "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "FFmpeg silence detection failed")[-4000:])
        log = proc.stderr or ""
        starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", log)]
        ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", log)]
        intervals = []
        for i, start in enumerate(starts):
            intervals.append({"start": start, "end": ends[i] if i < len(ends) else None})
        return {"path": source, "noise_threshold_db": noise, "minimum_duration": duration,
                "silence_intervals": intervals,
                "warning": "Silence detection is signal analysis, not a judgement that a pause should be cut."}
    if name == "transcribe_audio":
        import hashlib
        model_size = args.get("model") or "base"
        language = args.get("language") or ""
        digest = hashlib.sha256()
        with open(source, "rb") as media_file:
            for chunk in iter(lambda: media_file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        cache_key = hashlib.sha256(f"{digest.hexdigest()}|{model_size}|{language}".encode("utf-8")).hexdigest()
        cache_dir = os.path.join(DIR, "transcripts")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, cache_key + ".json")
        if os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as cached_file:
                cached = json.load(cached_file)
            cached["cache_hit"] = True
            cached["note"] = "Cached draft transcription; listen to recordings before making dialogue cuts."
            return cached
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("Optional dependency missing: install faster-whisper with `pip install faster-whisper`.") from exc
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, meta = model.transcribe(source, language=language or None, word_timestamps=True, vad_filter=True)
        result = []
        for seg in segments:
            result.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text,
                           "words": [{"start": round(w.start, 3) if w.start is not None else None,
                                      "end": round(w.end, 3) if w.end is not None else None,
                                      "word": w.word, "probability": w.probability} for w in (seg.words or [])]})
        payload = {"path": source, "source_sha256": digest.hexdigest(), "model": model_size,
                   "language": meta.language, "language_probability": meta.language_probability,
                   "segments": result, "cache_hit": False,
                   "note": "Transcription is a draft; listen to recordings before making dialogue cuts."}
        temp_path = cache_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as cache_file:
            json.dump(payload, cache_file, ensure_ascii=False)
        os.replace(temp_path, cache_path)
        return payload
    if name == "make_contact_sheet":
        ffmpeg = _binary("ffmpeg", "FFMPEG")
        interval = float(args.get("interval_seconds") or 3)
        columns = int(args.get("columns") or 4)
        if not 0.5 <= interval <= 60 or not 1 <= columns <= 8:
            raise ValueError("interval_seconds must be 0.5..60 and columns must be 1..8")
        output = _output_path(source, ".jpg")
        vf = f"fps=1/{interval},scale=320:-1,tile={columns}x4"
        _run_media_command([ffmpeg, "-nostdin", "-y", "-i", source, "-vf", vf,
                            "-frames:v", "1", "-q:v", "3", output], 600)
        with open(output, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return {"path": output, "image": encoded, "mimeType": "image/jpeg",
                "note": "Contact sheet is a visual sampling; inspect source footage before final shot decisions."}
    raise ValueError(f"unsupported media tool: {name}")


def run_tool(name, args):
    if name in {"probe_media", "extract_audio", "detect_silence", "transcribe_audio", "make_contact_sheet"}:
        try:
            result = run_media_tool(name, args)
            if name == "make_contact_sheet":
                return {"content": [{"type": "image", "data": result["image"], "mimeType": result["mimeType"]},
                                    {"type": "text", "text": json.dumps({k: v for k, v in result.items() if k not in {"image", "mimeType"}}, indent=2)}]}
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
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
