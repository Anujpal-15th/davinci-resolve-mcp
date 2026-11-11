# Resolve Creative Editor — Agent Operating Rules

## Server topology
- One MCP server is registered: `resolve-creative-editor`.
- `resolve__*` is the primary engine.
- `legacy__*` is a compatibility fallback only.
- `media__*` provides local media analysis.
- `edit__*` provides checked, read-back editing built on the bridge. Prefer it over raw `legacy__` calls.
- On free Resolve only `resolve__knowledge`, `resolve__media_analysis` and `resolve__setup` are exposed.
- Never mix `resolve__` and `legacy__` timeline mutations in one operation.

## Using the tools efficiently
- Start with `edit__timeline_summary`, then `edit__inspect_range` for just the part you will change. Don't list whole
  timelines repeatedly; reads are cached for a few seconds and every write clears the cache.
- Destructive `edit__` tools return a preview first. Show it, then repeat with `confirm=true`.
- Group several changes with `edit__batch` (one backup, stops at the first failure).
- Prefer `edit__set_enabled` (reversible) over deleting clips.
- Move and trim copy grade, transform, audio levels, fades, colour, flags and markers, but not keyframes.
- Never call Studio-only features on free Resolve: they open a dialog that blocks every later call.

## Editing workflow
1. Inspect Resolve version, project, timeline, frame rate, and media inventory.
2. Inspect existing analysis before repeating work.
3. Analyze source footage and cache transcripts by content hash.
4. Prepare an edit plan with source-frame ranges and clear rationale.
5. Present the cut list / major editorial choices before destructive mutation.
6. Duplicate/archive the target timeline before changes.
7. Execute small batches, read back the timeline, and compare expected vs actual clips.
8. Inspect rendered frames at representative cut points.
9. Review dialogue continuity, room tone, music balance, titles and captions.
10. Run final render/deliverable checks. Do not claim completion based only on a successful API response.

## Creative priorities
1. Story/emotion
2. Clarity
3. Rhythm
4. Continuity
5. Visual polish

Do not cut a pause merely because it is quiet. Preserve breaths, comic timing, reactions, and meaningful silence.
Do not choose a take from transcript text alone if delivery, emotion, or audio quality matters.
Do not choose music from metadata or beat analysis alone; audition it against the picture.

## Source-media policy
- Source camera files are read-only.
- Write transcripts, contact sheets, extracted audio and other derivatives to analysis/scratch locations.
- Never overwrite originals.
- Use explicit project/timeline names rather than an ambiguous "current" target where possible.

## Performance policy
- Keep the compound 37-tool server as default; do not enable the 390-tool granular mode unless a specific missing API needs it.
- Do not install heavyweight local models by default on the user's 8 GB RAM machine.
- Use CPU/int8 and small/base transcription models unless the user chooses otherwise.
- Avoid rerunning transcription when the SHA-256 cache key matches.
