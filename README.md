# Resolve Creative Editor

Edit video in **DaVinci Resolve** by talking to Claude. It works on your real project: it places and trims clips,
moves them without losing their grade, adds transitions, fades, captions and markers, sets audio levels, checks your
render, and shows you a preview before anything destructive.

It works with the **free version** of Resolve and with **Studio**, through
[MCP](https://modelcontextprotocol.io), so it plugs into Claude Code, Claude Desktop and other MCP clients.

> **Tested on:** Windows 11 with DaVinci Resolve **21.1.0.17 free**. Studio, macOS and Linux have not been tested
> by this project. See the [compatibility matrix](#what-works) for exactly what was checked in real Resolve.

## Which version of Resolve do you have?

| | Resolve **free** | Resolve **Studio** |
|---|---|---|
| Main tools | `edit__` (23), `legacy__` (29), `media__` (5) | the same, plus all 37 `resolve__` tools |
| One-time Resolve setting | None | Preferences > General > External scripting using > **Local** |
| Each time you open Resolve | Run one line in the Console ([step 3](#3-start-the-bridge)) | Nothing for `resolve__`; the line for `edit__` |

Blackmagic limits outside control of Resolve to Studio. On free, the server detects this and shows only the three
`resolve__` tools that work without it (`knowledge`, `media_analysis`, `setup`).

## What you need

- [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve) 21.1 or newer (transitions, fades,
  speed and normalization need 21.1)
- [Python 3.10 or newer](https://www.python.org/downloads/), available as `python`
- [Git](https://git-scm.com/downloads)
- [FFmpeg](https://ffmpeg.org/download.html) on your PATH (for `media__`, audio markers and file QC)
- [Claude Code](https://claude.com/claude-code) or another MCP client

## Install (Windows)

### 1. Download

Open a normal PowerShell window (not one inside another app) where you want to keep the project:

```powershell
git clone https://github.com/Anujpal-15th/davinci-resolve-mcp.git Resolve-Creative-Editor
cd Resolve-Creative-Editor
```

### 2. Run setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

Setup downloads the upstream engine at a pinned commit into `vendor\`, copies `resolve_bridge.lua` into Resolve's
scripts folder and into `%USERPROFILE%\resolve-claude-bridge`, registers the server with Claude Code if it's
installed, and finishes with a health check. Until you start the bridge, that check says `bridge: not running`.

| Option | Use it when |
|---|---|
| `.\setup.ps1 -Lite` | Free Resolve and you want a small, quick install without the upstream engine |
| `.\setup.ps1 -Studio` | You have Studio |
| `.\setup.ps1 -WithTranscription` | You want captions from speech (installs `faster-whisper`) |
| `.\setup.ps1 -NoRegister` | You'd rather register the server yourself |

**Claude Desktop** or other clients: add this to the MCP config (Settings > Developer > Edit Config), with your path:

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

Register only this one server, and remove any other Resolve MCP server so two tools don't edit the same timeline.
Restart your client after setup.

### 3. Start the bridge

Do this **each time you open Resolve**:

1. Open a project in Resolve.
2. Open **Workspace > Console** and choose the **Lua** tab.
3. Paste this line and press Enter:

```lua
dofile(os.getenv("USERPROFILE").."\\resolve-claude-bridge\\resolve_bridge.lua")
```

You should see `resolve_bridge: running`. Leave Resolve open. Running the line again restarts the bridge.

### 4. Try it

Ask Claude **"Give me a timeline summary."** If it lists your timeline's tracks, you're connected.

## What to ask for

- "Summarize this timeline and list any gaps."
- "Show me what's between 01:00:20:00 and 01:00:30:00, with clip settings."
- "Move the clip at 01:00:42:10 to 01:01:05:00." (you'll see a preview first)
- "Trim 10 frames off the start of that clip."
- "Add a 12-frame cross dissolve at the end of the second clip."
- "Fade the music out over 2 seconds and turn it down 6 dB."
- "Normalize the dialogue clips to YouTube loudness."
- "Add captions from interview.mp4." (needs `-WithTranscription`)
- "Put markers on the beats of song.wav."
- "Export the timeline as OTIO" or "Check my render D:\Exports\final.mp4 for loudness and black frames."

Claude makes a backup copy of the timeline before every move or trim, and asks before anything destructive.

## What works

**Live-tested** means it ran against real DaVinci Resolve 21.1.0.17 free on Windows (`tests/live_resolve.py`, 23
checks, all passing). **Automated** means it is covered by tests with a fake Resolve, which prove the logic but not
Resolve's behaviour.

| Area | Tool | Free 21.1 | Studio |
|---|---|---|---|
| Inspect | `edit__timeline_summary`, `edit__inspect_range`, `edit__find_media` | Live-tested | Not tested |
| Backup | `edit__snapshot_timeline` | Live-tested | Not tested |
| Place clips | `edit__place_clips` (refuses overwrites) | Live-tested | Not tested |
| Move, trim | `edit__move_item`, `edit__trim_item` (keeps grade, transform, audio level, fades, colour, flags, markers, link) | Live-tested | Not tested |
| Disable | `edit__set_enabled` | Live-tested | Not tested |
| Fades | `edit__set_fades` | Live-tested | Not tested |
| Transitions | `edit__add_transition` | Native call live-tested (Cross Dissolve) | Upstream-tested |
| Audio level, pan | `edit__set_audio` | Live-tested (volume) | Not tested |
| Loudness | `edit__normalize_audio` | Modes read live; normalization **not run** | Upstream-tested |
| Speed | `edit__set_speed` | Native call live-tested | Upstream-tested |
| Grade | `edit__copy_grade`, `legacy__set_grade`, `legacy__set_lut` | Grade copy live-tested inside move | Not tested |
| Markers | `edit__add_markers`, `edit__add_audio_markers` | Live-tested | Not tested |
| Captions | `edit__add_captions` (exact length Text+ cards) | Live-tested | Not tested |
| Subtitles | `edit__export_srt`, then File > Import > Subtitle | SRT file tested; import is manual | Same |
| Export | `edit__export_timeline` | OTIO live-tested; others not | Not tested |
| Render | `legacy__queue_render`, `legacy__start_render` (needs confirm) | Live-tested earlier | Not tested |
| Final check | `edit__qc_file` (LUFS, true peak, black, frozen, silence) | Tested on local files | Same |
| Media analysis | `media__*`, `resolve__media_analysis` | Tested on local files | Same |
| Editing guidance | `resolve__knowledge` | Works offline | Same |
| Full Resolve API | 34 other `resolve__` tools | Not available on free | Not tested by this project |

### Not possible through Resolve's scripting

- **Keyframes:** they can't be read, so a moved or trimmed clip loses them. Animated clips are better moved by hand.
- **Trimming in place:** free 21.1 has no `SetStart`/`SetEnd`, so move and trim rebuild the clip as described above.
- **Power windows, qualifiers, adding nodes:** use LUTs or CDL on a node, or grade by hand.
- **Subtitle tracks:** importing subtitles by script can crash Resolve 21, so you import the SRT yourself.
- **Fairlight effects and bus routing:** only clip volume, pan, fades and normalization are reachable.
- **Hearing audio:** Claude measures loudness, silence and beats but can't listen. Listen before final cuts.

## Safety

- Destructive `edit__` tools return a **preview** first and need `confirm=true` to run.
- Moves and trims make a **backup timeline**, build the replacement before removing the original, and put the clip
  back with its settings if anything fails.
- Every result says whether Resolve's read-back matched (`verification: passed`) or not.
- `legacy__delete_items` and `legacy__start_render` need `confirm=true`.
- Every change is logged in `%USERPROFILE%\resolve-claude-bridge\audit.jsonl` (`edit__audit_log`).
- Studio-only features are never called on free, because they open a dialog that blocks Resolve.

## Recovery

| Situation | What to do |
|---|---|
| An edit went wrong | Press Ctrl+Z in Resolve, or open the `... before edit <time>` timeline Claude made |
| Too many backup timelines | Delete the old `before edit` timelines in the Media Pool |
| Captions need different text | Edit the caption timeline in the `Captions (Resolve Creative Editor)` bin; it updates on the timeline |
| Resolve stopped answering | A dialog may be open in Resolve. Close it, then restart the bridge (step 3) |
| Roll back this project | `git checkout <older commit>` in the project folder, then run `.\setup.ps1` again |

## Troubleshooting

| Problem | Fix |
|---|---|
| `Bridge not running` | Repeat [step 3](#3-start-the-bridge). The bridge stops when Resolve closes. |
| `SCRIPTING_UNAVAILABLE` from `resolve__` tools | On free, use `edit__` tools. On Studio, set External scripting to Local and restart Resolve. |
| `resolve_bridge` does nothing from the Scripts menu | Use the Console line in step 3. |
| `dofile` says the file isn't found | Run `.\setup.ps1` again from a normal PowerShell window. |
| `Run setup.ps1 first` | Run `python unified_server.py --doctor`; all `_exists` values should be `true`. |
| `ffprobe was not found` | Install FFmpeg, add it to PATH, restart your client. |
| A move or trim is refused | Read the reason: speed changes, Fusion clips, transitions and mixed frame rates are refused on purpose. |
| New tools don't appear | Restart your MCP client. |

## How it works

```
Claude ──> unified_server.py ─┬─> resolve__  upstream engine ──────────────> Resolve Studio
                              ├─> legacy__   mcp_server.py ──┐
                              ├─> edit__     editor_tools.py ─┴─ files ──> resolve_bridge.lua (inside Resolve)
                              └─> media__    FFmpeg / faster-whisper on your files
```

Free Resolve only runs scripts from inside the app, so `resolve_bridge.lua` runs in Resolve's Console and carries
out a fixed list of operations (there is no "run any code" tool). Requests travel as `request.lua` in
`%USERPROFILE%\resolve-claude-bridge`; Resolve's Lua can't write files, so replies come back through Resolve's own
`Fusion.prefs`. No network port is opened. `edit__` tools combine bridge calls with checks, previews and read-back.

Optional environment variables: `RESOLVE_MCP_REPO`, `RESOLVE_MCP_PYTHON`, `RESOLVE_MCP_SERVER`, `RESOLVE_BRIDGE_DIR`,
`RCE_UPSTREAM_TOOLS` (`auto`, `all` or `offline`), `FFMPEG`, `FFPROBE`.

## Tests

**Automated** (no Resolve needed):

```powershell
python -m unittest discover -s tests -v
python -m pip install lupa
python test_smoke.py
```

**Live** (needs Resolve open with a project and the bridge running; works only on a new `_RCE_LIVE_` scratch
timeline, checks your current timeline is unchanged, then deletes the scratch timeline):

```powershell
python tests/live_resolve.py
```

## Credits and license

MIT licensed, see [LICENSE](LICENSE). The upstream engine is
[samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp) (MIT), downloaded by setup
at a pinned commit and left unmodified. The result-envelope design, the nested-timeline title route and the Resolve
21.1 API notes come from that project. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Optional
`faster-whisper` is MIT; FFmpeg is LGPL/GPL and is used as a separate program, not bundled. DaVinci Resolve is a
trademark of Blackmagic Design; this project is not affiliated with them.
