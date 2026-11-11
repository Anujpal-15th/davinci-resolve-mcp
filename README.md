# Resolve Creative Editor

Edit video in **DaVinci Resolve** by talking to Claude. Ask it to import footage, cut clips onto a timeline,
add markers, grade, check frames and render, and it makes those changes in your real Resolve project.

It works with the **free version** of Resolve and with **Studio**. It is an
[MCP](https://modelcontextprotocol.io) server, so it plugs into Claude Code, Claude Desktop and other MCP clients.

> **Status:** live-tested on Windows 11 with DaVinci Resolve 21.1 (free). macOS and Linux are not tested, and
> the free-version bridge below is Windows only.

## Which version of Resolve do you have?

| | Resolve **free** | Resolve **Studio** |
|---|---|---|
| Tools that control Resolve | `legacy__` (29 tools) | `resolve__` (37 tools), `legacy__` as a backup |
| One-time Resolve setting | None | Preferences > General > External scripting using > **Local** |
| Each time you open Resolve | Run one line in the Console ([step 4](#4-free-version-start-the-bridge)) | Nothing |
| `media__` tools (5) | Yes | Yes |

Blackmagic limits outside control of Resolve to Studio. On the free version, `resolve__` tools are listed but
answer `SCRIPTING_UNAVAILABLE`. That is expected: use the `legacy__` tools, which run from inside Resolve.

## What you need

- [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve)
- [Python 3.10 or newer](https://www.python.org/downloads/), available as `python` in your terminal
- [Git](https://git-scm.com/downloads)
- [Claude Code](https://claude.com/claude-code) or another MCP client
- [FFmpeg](https://ffmpeg.org/download.html) on your PATH (only for the `media__` tools)

## Install (Windows)

### 1. Download the project

Open PowerShell in the folder where you want to keep it. Don't move the folder afterwards.

```powershell
git clone https://github.com/Anujpal-15th/davinci-resolve-mcp.git Resolve-Creative-Editor
cd Resolve-Creative-Editor
```

### 2. Run setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

This takes a few minutes. It downloads a pinned version of the upstream engine into `vendor\`, installs its
Python packages there, and copies `resolve_bridge.lua` into Resolve's scripts folder (backing up any older copy).

Near the end you may see `the scripting bridge returned no connection`. On the free version that is normal.

### 3. Connect it to Claude

**Claude Code**, from inside the project folder:

```powershell
claude mcp add --scope user resolve-creative-editor -- python -X utf8 "$PWD\unified_server.py"
```

**Claude Desktop** (Settings > Developer > Edit Config) or other clients, using your real path:

```json
{
  "mcpServers": {
    "resolve-creative-editor": {
      "command": "python",
      "args": ["-X", "utf8", "C:\\path\\to\\Resolve-Creative-Editor\\unified_server.py"]
    }
  }
}
```

Register only this one server. If you have another Resolve MCP server set up, remove it so two tools don't
control the same timeline. Then restart your client.

### 4. Free version: start the bridge

Do this **each time you open Resolve**. Studio users can skip it.

1. Open a project in Resolve.
2. Open **Workspace > Console** and choose the **Lua** tab.
3. Paste this line and press Enter:

```lua
dofile(os.getenv("APPDATA").."\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\resolve_bridge.lua")
```

You should see `resolve_bridge: running`. Leave Resolve open. Running the line again restarts the bridge.
Use the Console, because the Workspace > Scripts menu may not start it.

### 5. Try it

Ask Claude:

> Get the Resolve state.

If it answers with your project and timeline name, you're connected.

## What to ask for

- "List my media and tell me which clips are longest."
- "Duplicate this timeline, then remove the clip at 00:01:20 on the copy."
- "Add a green marker at every silence longer than one second in interview.mp4."
- "Make a contact sheet of drone.mp4 so you can pick the best shots."
- "Show me the frame at 01:00:10:00."
- "Render this timeline to D:\Exports with the YouTube 1080p preset."

Tip: ask Claude to work on a **duplicate timeline**, and save your project before big changes.

## Tools

Names start with the group they belong to.

| Group | What it does | Tools |
|---|---|---|
| `resolve__` | Full engine, **Studio only** | 37 grouped tools: project, media pool, timeline, markers, color, Fusion, render and more |
| `legacy__` project | Works on free | `get_state`, `list_timelines`, `set_current_timeline`, `open_page`, `save_project` |
| `legacy__` media | | `list_media`, `import_media` |
| `legacy__` timeline | | `create_timeline`, `duplicate_timeline`, `add_track`, `append_clips`, `list_timeline_items`, `set_item_properties`, `delete_items` |
| `legacy__` markers | | `add_marker`, `list_markers`, `delete_marker`, `goto_timecode` |
| `legacy__` color | | `set_grade`, `set_lut`, `refresh_luts`, `probe_color` |
| `legacy__` titles | | `add_text_plus`, `style_text_plus` |
| `legacy__` review, render | | `export_frame`, `list_render_presets`, `queue_render`, `start_render`, `render_status` |
| `media__` | Reads files with FFmpeg, never changes them | `probe_media`, `extract_audio`, `detect_silence`, `make_contact_sheet`, `transcribe_audio` |

`transcribe_audio` needs one extra install. The model downloads on first use; `tiny` or `base` suit 8 GB of RAM.

```powershell
python -m pip install faster-whisper
```

## Limits on the free version

- There is no razor or direct move. Claude cuts by placing part of a clip (`end_frame` is exclusive), and moves
  by deleting and placing it again.
- No transitions, keyframes or power windows through scripting. Grading is CDL values or `.cube` LUTs on a node.
- Claude can't hear audio. Transcripts and silence detection are measurements, so listen before final cuts.
- Each bridge call rewrites Resolve's `Fusion.prefs`, which is fine for normal editing but not thousands of calls.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Bridge not running` | Repeat [step 4](#4-free-version-start-the-bridge). The bridge stops when Resolve closes. |
| `SCRIPTING_UNAVAILABLE` from `resolve__` tools | On free, use `legacy__` tools. On Studio, set External scripting to Local and restart Resolve. |
| `resolve_bridge` isn't in the Scripts menu, or does nothing | Use the Console line in step 4. |
| Server won't connect, `Run setup.ps1 first` | Run setup again, then run `python unified_server.py --doctor`. All `_exists` values should be `true`. |
| `ffprobe was not found` | Install FFmpeg, add it to PATH, restart your client. |
| `python` isn't recognized | Reinstall Python with "Add to PATH" ticked. |
| New tools don't appear | Restart your MCP client. |

## How it works

```
Claude ──> unified_server.py ─┬─> resolve__  upstream engine ──> Resolve Studio
                              ├─> legacy__   mcp_server.py ──files──> resolve_bridge.lua (inside Resolve)
                              └─> media__    FFmpeg / faster-whisper
```

The legacy bridge opens no network port and has no "run any code" tool, only a fixed list of operations.
It exchanges small files through `%USERPROFILE%\resolve-claude-bridge`, which is also where extracted audio,
contact sheets and transcripts are saved. Your source media is never modified.

Optional settings (environment variables): `RESOLVE_MCP_REPO`, `RESOLVE_MCP_PYTHON`, `RESOLVE_MCP_SERVER`,
`RESOLVE_BRIDGE_DIR`, `FFMPEG`, `FFPROBE`.

## Run the tests

No Resolve needed:

```powershell
python -m unittest discover -s tests -v
python -m pip install lupa
python test_smoke.py
```

## Credits and license

MIT licensed, see [LICENSE](LICENSE). The primary engine is
[samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp) (MIT), downloaded by setup
at a pinned commit and left unmodified. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
DaVinci Resolve is a trademark of Blackmagic Design; this project is not affiliated with them.
