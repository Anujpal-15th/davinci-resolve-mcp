# DaVinci Resolve (free) MCP server

Let Claude (or any MCP client) edit in **DaVinci Resolve's free version**: import media, cut clips onto
timelines, build multi-track layouts, add markers, grade with CDL or custom LUTs, look at frames, and render.

Built and tested on **Windows 11 + DaVinci Resolve 21.1 (free)**.

## How it works

```
Claude ──stdio──> mcp_server.py ──request.lua──> resolve_bridge.lua (inside Resolve) ──> Resolve API
       <─────────────────────────── Fusion.prefs <──────────────────────────────────────┘
```

- `resolve_bridge.lua` runs **inside Resolve**. The free version allows scripts from inside the app but not
  external scripting, so the bridge is a Lua loop that runs a **fixed allowlist** of operations. There is no
  "run any code" tool.
- `mcp_server.py` is the MCP server your client launches (Python standard library only, nothing to install).
- They talk through files, no network port:
  - requests: `%USERPROFILE%\resolve-claude-bridge\request.lua`, read by the bridge with `loadfile`
  - replies: one `claude_bridge_res` entry in Resolve's `Fusion.prefs`

Two Windows quirks shaped this design:

1. **Resolve's Lua cannot write files.** There is no `io` library, and `bmd.writestring` only serialises
   values. The only thing that reaches disk is `fusion:SavePrefs()`, so replies travel through `Fusion.prefs`
   (hex-encoded JSON).
2. **The Claude desktop app is MSIX-packaged.** Windows redirects anything it, or a server it launches, writes
   under `AppData\Local` into a private package folder that Resolve cannot see. So requests go to
   `%USERPROFILE%\resolve-claude-bridge` instead.

## Install

1. Copy the bridge into Resolve's script folder:
   ```powershell
   Copy-Item resolve_bridge.lua "$env:APPDATA\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
   ```
2. Register the server with your MCP client (Python 3 on PATH).

   **Claude Code**, available in every folder:
   ```bash
   claude mcp add --scope user davinci -- python "C:\path\to\mcp_server.py"
   ```
   **Claude Desktop** (Settings > Developer > Edit Config) or other clients:
   ```json
   { "mcpServers": { "davinci": { "command": "python", "args": ["C:\\path\\to\\mcp_server.py"] } } }
   ```

## Start the bridge (each time you open Resolve)

Open a project, then **Workspace > Console**, choose the **Lua** tab and run:

```lua
dofile(os.getenv("APPDATA").."\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\resolve_bridge.lua")
```

You should see `resolve_bridge: running`. It keeps running in the background. Run it again to restart (the old
copy stops by itself). The Workspace > Scripts menu may not start it, so use the Console.

## Tools (27)

| Area | Tools |
|---|---|
| State | `get_state`, `list_timelines`, `set_current_timeline`, `open_page`, `save_project` |
| Media | `list_media`, `import_media` |
| Timeline | `create_timeline`, `duplicate_timeline`, `add_track`, `append_clips`, `list_timeline_items`, `set_item_properties`, `delete_items` |
| Markers / playhead | `add_marker`, `list_markers`, `delete_marker`, `goto_timecode` |
| Colour | `set_grade` (ASC CDL), `set_lut`, `refresh_luts`, `probe_color` |
| Look at it | `export_frame` (returns the frame as an image) |
| Render | `list_render_presets`, `queue_render`, `start_render`, `render_status` |

Editing tips:

- **Cutting** = `append_clips` with `start_frame` / `end_frame` (source frames) and `record_frame` (timeline
  frame). The end frame acts as exclusive.
- **Tiles / grids**: put copies on higher tracks and use `set_item_properties` with `CropLeft/Right/Top/Bottom`
  (pixels) or `ZoomX/ZoomY` + `Pan/Tilt`.
- **Still images** get Resolve's default 5-second length whatever range you pass. Use a short video clip for
  flashes.
- **Grading**: `set_lut` loads any `.cube` file onto a node. Put LUTs in Resolve's LUT folder and call
  `refresh_luts` first. Scripts cannot add nodes or draw power windows; colour-selective work can be baked
  into a LUT.

## Known limits

- Free Resolve may block some API calls; they come back as tool errors.
- No razor, transitions, keyframes, titles or power windows through the scripting API.
- Replies rewrite `Fusion.prefs` twice per call: fine for interactive editing, not for thousands of calls.
- Windows only so far (paths use `%APPDATA%` / `%USERPROFILE%`).

## Test

`test_smoke.py` runs the real bridge against a fake Resolve, through the real MCP server (no Resolve needed):

```bash
pip install lupa
python test_smoke.py
```

## License

MIT
