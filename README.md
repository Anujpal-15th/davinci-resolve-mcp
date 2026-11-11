# Resolve Creative Editor — Unified MCP

A single MCP entry point combining:

1. **Samuel Gursky's DaVinci Resolve MCP** as the primary Resolve engine.
2. **Your original Lua bridge + media-analysis tools** as a compatibility/fallback layer.
3. **A single gateway** that exposes both backends to Claude Code with namespaced tools.

The upstream repository is pinned to commit `8f9e488a1be0d1bd8be418cc4243b695781061cd` (2026-11-11).
It is cloned into `vendor/davinci-resolve-mcp/` during setup; it is not copied or modified.
This keeps upstream updates auditable and preserves its MIT notices.

## What "one" means

Claude Code registers **one MCP server**: `resolve-creative-editor`.

- `resolve__...` — primary upstream Resolve tools (37 compound tools).
- `legacy__...` — your original Lua bridge tools, retained as a fallback.
- `media__...` — your FFmpeg / transcription / contact-sheet tools.

The gateway routes calls to the correct backend. Namespacing prevents tool-name collisions.
Do not use the legacy and primary Resolve editing tools to mutate the same timeline in the same operation.
Prefer `resolve__` for editing; use `legacy__` only when the primary server cannot connect on your Resolve build.

## Why this design

Blindly copying two MCP servers together would duplicate Resolve controls, create naming collisions,
and make it unclear which server owns the timeline. The gateway gives one Claude connection while
keeping the two implementations isolated and recoverable.

## Features

### Primary Resolve engine (upstream)

- Compound Resolve tools for project, media, timeline, editing, markers, color, Fusion, audio and rendering.
- Guarded operations, dry runs, confirmation tokens, timeline archiving and readback verification.
- Media analysis, transcript editing, shot/take analysis and edit planning.
- Offline advanced Node server remains available in the upstream checkout for workflows that need it.

### Your retained modules

- Free-edition in-app Lua bridge.
- Media probe via `ffprobe`.
- Audio extraction via FFmpeg.
- Silence detection.
- Optional local transcription via `faster-whisper`.
- Contact-sheet generation.
- Original smoke tests and patch notes.

Transcription is cached by source-file SHA-256 + model + language in
`%USERPROFILE%\\resolve-claude-bridge\\transcripts\\`. The original media is never modified.

## Windows setup

Requirements:
- Git
- Python 3.10+ (Python 3.12 is the conservative choice)
- DaVinci Resolve
- FFmpeg + FFprobe on PATH for the media tools

From PowerShell in this folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\\setup.ps1
```

The setup script:
1. Fetches the pinned upstream commit into `vendor/davinci-resolve-mcp`.
2. Runs the upstream installer with `--clients manual` (does not configure a second MCP entry).
3. Copies your Lua bridge into Resolve's Utility scripts folder, backing up an existing copy.
4. Writes a project `.mcp.json` containing exactly one MCP server entry.

Restart Claude Code after setup.

## Start the free-edition legacy bridge

If the upstream server cannot connect on your Resolve Free build, open a project in Resolve,
open Workspace > Console, choose Lua, and run:

```lua
dofile(os.getenv("APPDATA").."\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\resolve_bridge.lua")
```

The bridge must remain running while using `legacy__` tools.

Important: Resolve Free scripting support varies by exact Resolve patch. The upstream docs state
that Free 21.1 may no longer expose the in-app Python script route. Your original Lua bridge is
retained as a fallback, but it must be live-tested on your exact build. If the upstream server
connects successfully, prefer `resolve__` tools and leave `legacy__` as fallback.

## Optional transcription

The media transcription tool requires `faster-whisper`. Install it into the Python environment
used by the gateway:

```powershell
python -m pip install faster-whisper
```

For an 8 GB RAM laptop, start with `base` or `tiny`, CPU/int8. Model weights download on first use.
Do not install WhisperX, Transformers, OpenCLIP or other heavy extras unless a workflow needs them.

## Safety and workflow policy

- Never overwrite or alter camera originals.
- Analyze into sidecar/cache folders.
- Start with read-only probes and analysis.
- Show a proposed cut list / edit plan before timeline mutation.
- Keep dialogue breaths, punchline pauses and meaningful reactions.
- Never delete all detected silence automatically; room tone and intentional pauses matter.
- Use a duplicate/archived timeline for destructive edits.
- Read back timeline structure after every edit and inspect rendered frames.
- Do not claim that a model listened to music unless an actual audio-perception path was used.
- Human approval is required for subjective take selection, music choice, color look and final export.
- For grades, inspect representative frames before and after; metadata alone is not visual verification.

## Configuration

The gateway reads:
- `RESOLVE_MCP_REPO` — upstream checkout (defaults to `vendor/davinci-resolve-mcp`).
- `RESOLVE_MCP_PYTHON` — upstream venv Python (auto-detected if omitted).
- `RESOLVE_MCP_SERVER` — upstream `src/server.py` (auto-detected if omitted).
- `RESOLVE_BRIDGE_DIR` — legacy bridge IPC/cache folder (defaults to `%USERPROFILE%\\resolve-claude-bridge`).

For diagnostics, run:

```powershell
python unified_server.py --doctor
```

## Tests

```powershell
python -m unittest discover -s tests -v
python -m py_compile unified_server.py mcp_server.py
```

The tests use fake MCP child processes. They do not require Resolve to be running.
A live Resolve validation is still required before production use.

## Upstream

- https://github.com/samuelgursky/davinci-resolve-mcp
- Pinned commit: `8f9e488a1be0d1bd8be418cc4243b695781061cd`
- Upstream license: MIT (see `THIRD_PARTY_NOTICES.md` and upstream `LICENSE`).
