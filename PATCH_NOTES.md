# Patch notes — DaVinci Resolve MCP

## Stage-two audit (2026-11-11)

Found and fixed, each confirmed in a real Resolve 21.1 (free) on a scratch timeline:
- Move and trim deleted the clip first and re-added it, losing grade, transform, audio levels and fades, and a failed
  re-add could lose the clip. They now build the replacement on a temporary track, copy grade (`CopyGrades`),
  properties, fades, colour, flags and markers, verify, and only then remove the original. Linked audio/video moves
  with it and is relinked. Keyframes still can't be copied: the API has no way to read them.
- Speed-changed clips, Fusion clips, take clips, mixed frame rates and clips touching a transition are refused
  instead of being replaced with the wrong source range.
- Text+ captions were always 5 s long (the API can't resize titles) and drifted at 29.97/23.976 because the frame
  rate was rounded down. Captions now use upstream's nested-timeline route for exact frames and lengths; long lines
  are split and overlaps trimmed. Timecode maths is exact, including drop frame.
- Timeline reports counted transitions as overlaps and returned every gap; they are now compact with timecodes.
- Duplicating a timeline makes Resolve switch to the copy, sometimes after the script switched back. Timeline
  switching is now verified and retried, and every confirmed edit checks the timeline didn't change underneath it.
- The bridge path in the README didn't work when setup ran inside a sandboxed app; the bridge now also lives in
  `%USERPROFILE%\resolve-claude-bridge`, and the bridge can reload itself.

Added: previews before destructive edits, `edit__batch`, read caching with invalidation, `place_clips`,
`inspect_range`, `find_media`, native fades, transitions, speed, audio volume/pan, loudness normalization,
grade copy, batch markers, timeline export (OTIO/FCPXML/EDL/AAF/DRT), SRT export and file QC.
Not added because the API can't do it: keyframes, power windows, node creation, subtitle-track placement
(scripted subtitle import crashes Resolve 21), Fairlight effects and bus routing.

## Fixed
- `add_track` now uses an explicit branch. The previous Lua `and/or` idiom could call `AddTrack("video")` when adding an audio track returned false/nil.
- `append_clips` validates source ranges, track indexes, record positions and media type before calling Resolve.
- `AppendToTimeline` ranges are treated as half-open `[start_frame, end_frame)`. `end_frame` is exclusive; default full-clip end is the source frame count. This prevents one-frame trims/gaps from inclusive-bound assumptions.
- Lua JSON decoding now handles four-byte UTF-8 code points and UTF-16 surrogate pairs, and rejects malformed unicode/control escapes.
- Smoke tests now cover the exclusive end-frame behavior and invalid zero-length ranges.

## Added
- `probe_media`: ffprobe metadata inspection.
- `extract_audio`: WAV extraction for auditioning / downstream analysis.
- `detect_silence`: FFmpeg silence intervals (not automatic edit decisions).
- `transcribe_audio`: optional faster-whisper transcription with word timestamps.
- `make_contact_sheet`: contact-sheet image for visual shot review.

## Verification performed here
- Python syntax compilation passed.
- MCP tool registration reports 32 tools.
- FFmpeg integration smoke-tested against a generated sample: metadata probe, WAV extraction, silence detection and contact-sheet generation passed.
- Full Lua-to-Resolve smoke test was not run: `lupa` is not installed in this environment and DaVinci Resolve is not available here.
- No live timeline edit, Fairlight operation, color grade or render was performed.

## Still missing for a genuinely professional autonomous editor
This remains a safer Resolve bridge plus media-analysis helpers, not a complete AI editor. It still needs:
- a real audio audition/perception path (transcription and waveform statistics are not listening);
- take comparison and dialogue assembly logic;
- deeper Resolve/Fairlight operations verified against the installed Resolve version;
- title/subtitle and transition workflows;
- visual grading analysis and node-level grade execution;
- render playback review and iterative correction.

Do not claim the AI listened to a file unless an audio-capable model or human actually auditioned it.

## Unified integration (2026-11-11)
- Added `unified_server.py`: one MCP stdio gateway for the upstream 37-tool compound server, the original Lua bridge, and local media-analysis tools.
- Namespaced tool families (`resolve__`, `legacy__`, `media__`) to prevent collisions.
- Retained the legacy bridge as a fallback rather than mixing two timeline controllers silently.
- Added SHA-256 + model + language keyed transcription caching; source media remains read-only.
- Added Windows setup that pins the upstream repository revision, installs its dependencies without registering a second MCP, copies the Lua bridge with backup, and writes one `.mcp.json` entry.
- Gateway routing tested against fake MCP child processes. Live Resolve validation is still required.

## Editor upgrade (2026-11-11)
- Added `editor_tools.py`: `edit__` tools with pre-checks, read-back verification, rollback and an audit log. The result envelope idea comes from upstream.
- Gateway keeps running if the upstream engine is missing, hides upstream tools that cannot work on free Resolve, and sends agent instructions at startup.
- `delete_items` and `start_render` now need `confirm=true`.
- Setup registers the server itself and adds `-Lite`, `-Studio`, `-WithTranscription` and `-NoRegister`.
