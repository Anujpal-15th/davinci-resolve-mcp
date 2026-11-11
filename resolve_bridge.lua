-- resolve_bridge.lua: runs INSIDE DaVinci Resolve. Start it from Workspace > Console (Lua tab) with
--   dofile(os.getenv("APPDATA").."\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility\\resolve_bridge.lua")
-- mcp_server.py writes request.lua (`return [==[<json>]==]`) into %USERPROFILE%\resolve-claude-bridge\;
-- this loop runs it against a fixed allowlist of Resolve operations.
-- No arbitrary code execution. Run it again to restart; the old loop stops by itself.
--
-- Resolve's Lua has no io and no file-writing call (bmd.writestring only serialises). The only reader is
-- loadfile, and the only writer is fusion:SetData + fusion:SavePrefs(), which writes Fusion.prefs.
-- So the reply is stored as `claude_bridge_res = "<id>:<hex json>"` in Fusion.prefs and mcp_server.py reads it.
-- An empty payload ("<id>:") is the ack that tells mcp_server.py the bridge is alive.
-- ponytail: rewrites Fusion.prefs twice per request; fine for interactive use, not for high call rates.

-- Not under AppData: the Claude desktop app is MSIX-packaged, so its writes to AppData\Local get redirected
-- into its private package folder where Resolve cannot see them.
local DIR = BRIDGE_DIR or (os.getenv("USERPROFILE") .. "\\resolve-claude-bridge\\")
local REQ = DIR .. "request.lua"
local fu = fusion or fu or (resolve or Resolve()):Fusion()

local function readreq()
  local chunk = loadfile(REQ)  -- nil when missing
  if not chunk then return nil end
  local ok, s = pcall(chunk)
  if ok and type(s) == "string" and s ~= "" then return s end
  return nil
end

local function hex(s) return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end)) end
local function reply(id, payload)
  fu:SetData("claude_bridge_res", id .. ":" .. (payload and hex(payload) or ""))
  fu:SavePrefs()
end

local function log(msg) print("resolve_bridge: " .. tostring(msg)) end

local function main()
log("starting")
local resolve = resolve or Resolve()
if not resolve then error("no resolve object available to this script") end
local sleep = bmd.wait or function(s) local t = os.clock() + s while os.clock() < t do end end

-- ---------- minimal JSON ----------
local json = {}
do
  local function skip(s, i) return s:find("%S", i) or #s + 1 end
  local esc = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }
  local function utf8char(cp)
    assert(cp and cp >= 0 and cp <= 0x10FFFF, "invalid unicode code point")
    if cp < 0x80 then
      return string.char(cp)
    elseif cp < 0x800 then
      return string.char(0xC0 + math.floor(cp / 64), 0x80 + cp % 64)
    elseif cp < 0x10000 then
      return string.char(0xE0 + math.floor(cp / 4096), 0x80 + math.floor(cp / 64) % 64, 0x80 + cp % 64)
    end
    return string.char(0xF0 + math.floor(cp / 262144),
      0x80 + math.floor(cp / 4096) % 64,
      0x80 + math.floor(cp / 64) % 64,
      0x80 + cp % 64)
  end
  local function read_hex4(s, i)
    local hex = s:sub(i, i + 3)
    assert(#hex == 4 and hex:match("^%x%x%x%x$"), "invalid unicode escape")
    return tonumber(hex, 16)
  end
  local function dstring(s, i)
    assert(s:sub(i, i) == '"', "expected string")
    local out, j = {}, i + 1
    while true do
      local c = s:sub(j, j)
      if c == '"' then return table.concat(out), j + 1 end
      assert(c ~= "", "unterminated string")
      if c == "\\" then
        local n = s:sub(j + 1, j + 1)
        if n == "u" then
          local cp = read_hex4(s, j + 2)
          j = j + 6
          if cp >= 0xD800 and cp <= 0xDBFF then
            assert(s:sub(j, j + 1) == "\\u", "missing low surrogate")
            local low = read_hex4(s, j + 2)
            assert(low >= 0xDC00 and low <= 0xDFFF, "invalid low surrogate")
            cp = 0x10000 + (cp - 0xD800) * 0x400 + (low - 0xDC00)
            j = j + 6
          else
            assert(not (cp >= 0xDC00 and cp <= 0xDFFF), "unexpected low surrogate")
          end
          out[#out + 1] = utf8char(cp)
        else
          out[#out + 1] = assert(esc[n], "bad escape")
          j = j + 2
        end
      else
        assert(string.byte(c) >= 0x20, "unescaped control character")
        out[#out + 1] = c
        j = j + 1
      end
    end
  end
  local function dvalue(s, i)
    i = skip(s, i)
    local c = s:sub(i, i)
    if c == "{" then
      local t = {}
      i = skip(s, i + 1)
      if s:sub(i, i) == "}" then return t, i + 1 end
      while true do
        local k
        k, i = dstring(s, skip(s, i))
        i = skip(s, i)
        assert(s:sub(i, i) == ":", "expected :")
        t[k], i = dvalue(s, i + 1)
        i = skip(s, i)
        c = s:sub(i, i)
        if c == "}" then return t, i + 1 end
        assert(c == ",", "expected ,")
        i = i + 1
      end
    elseif c == "[" then
      local t, n = {}, 0
      i = skip(s, i + 1)
      if s:sub(i, i) == "]" then return t, i + 1 end
      while true do
        n = n + 1
        t[n], i = dvalue(s, i)
        i = skip(s, i)
        c = s:sub(i, i)
        if c == "]" then return t, i + 1 end
        assert(c == ",", "expected ,")
        i = i + 1
      end
    elseif c == '"' then return dstring(s, i)
    elseif s:sub(i, i + 3) == "true" then return true, i + 4
    elseif s:sub(i, i + 4) == "false" then return false, i + 5
    elseif s:sub(i, i + 3) == "null" then return nil, i + 4
    else
      local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
      assert(num and #num > 0, "bad json at " .. i)
      return tonumber(num), i + #num
    end
  end
  function json.decode(s) return (dvalue(s, 1)) end

  local function is_array(t)
    local n = 0
    for k in pairs(t) do
      if type(k) ~= "number" then return false end
      n = n + 1
    end
    return n == #t
  end
  function json.encode(v)
    local t = type(v)
    if v == nil then return "null"
    elseif t == "boolean" then return tostring(v)
    elseif t == "number" then
      if v ~= v or v == math.huge or v == -math.huge then return "null" end
      if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
      return string.format("%.17g", v)
    elseif t == "string" then
      return '"' .. (v:gsub('[%c"\\]', function(ch) return string.format("\\u%04x", ch:byte()) end)) .. '"'
    elseif t == "table" then
      local out = {}
      if next(v) == nil then return "[]" end
      if is_array(v) then
        for i = 1, #v do out[i] = json.encode(v[i]) end
        return "[" .. table.concat(out, ",") .. "]"
      end
      for k, val in pairs(v) do out[#out + 1] = json.encode(tostring(k)) .. ":" .. json.encode(val) end
      return "{" .. table.concat(out, ",") .. "}"
    end
    return json.encode(tostring(v)) -- Resolve objects etc.
  end
end

-- ---------- Resolve helpers ----------
-- Resolve lists carry extra non-index keys (e.g. __flags = <number>); keep only numeric keys, in order.
local function each(t)
  local keys, r = {}, {}
  if type(t) == "table" then for k in pairs(t) do if type(k) == "number" then keys[#keys + 1] = k end end end
  table.sort(keys)
  for _, k in ipairs(keys) do r[#r + 1] = t[k] end
  return r
end

local function proj()
  local p = resolve:GetProjectManager():GetCurrentProject()
  if not p then error("No project open in Resolve", 0) end
  return p
end

local function tl()
  local t = proj():GetCurrentTimeline()
  if not t then error("No current timeline", 0) end
  return t
end

local function pool() return proj():GetMediaPool() end

local function all_clips(folder, out)
  out = out or {}
  folder = folder or pool():GetRootFolder()
  for _, c in ipairs(each(folder:GetClipList())) do out[#out + 1] = c end
  for _, f in ipairs(each(folder:GetSubFolderList())) do all_clips(f, out) end
  return out
end

local function find_clip(ref)
  local by_id, by_name = {}, {}
  for _, c in ipairs(all_clips()) do
    if c:GetMediaId() == ref then by_id[#by_id + 1] = c end
    if c:GetName() == ref then by_name[#by_name + 1] = c end
  end
  local hit = (#by_id > 0) and by_id or by_name
  if #hit == 0 then error("clip not found: " .. tostring(ref), 0) end
  if #hit > 1 then error("ambiguous clip name " .. tostring(ref) .. "; use its id", 0) end
  return hit[1]
end

local function clip_info(c)
  local p = c:GetClipProperty() or {}
  return { id = c:GetMediaId(), name = c:GetName(), type = p["Type"], duration = p["Duration"],
           frames = p["Frames"], fps = p["FPS"], resolution = p["Resolution"], path = p["File Path"] }
end

local function item_info(it, kind, track)
  local okl, left = pcall(function() return it:GetLeftOffset() end)
  return { id = it:GetUniqueId(), name = it:GetName(), start = it:GetStart(), ["end"] = it:GetEnd(),
           duration = it:GetDuration(), source_start = okl and left or nil, track_type = kind, track = track }
end

local function items(kind)
  local t, out = tl(), {}
  for i = 1, (t:GetTrackCount(kind) or 0) do
    for _, it in ipairs(each(t:GetItemListInTrack(kind, i))) do out[#out + 1] = { it, kind, i } end
  end
  return out
end

local function find_item(id)
  for _, kind in ipairs({ "video", "audio", "subtitle" }) do
    for _, x in ipairs(items(kind)) do
      if x[1]:GetUniqueId() == id then return x[1] end
    end
  end
  error("timeline item not found: " .. tostring(id), 0)
end

local PROPS = {}
for _, k in ipairs({ "Pan", "Tilt", "ZoomX", "ZoomY", "ZoomGang", "Opacity", "RotationAngle", "Pitch", "Yaw",
  "CropLeft", "CropRight", "CropTop", "CropBottom", "FlipX", "FlipY", "CompositeMode", "RetimeProcess" }) do
  PROPS[k] = true
end
local PAGES = { media = true, cut = true, edit = true, fusion = true, color = true, fairlight = true, deliver = true }

-- ---------- operations (allowlist) ----------
local OPS = {}

function OPS.get_state()
  local p = resolve:GetProjectManager():GetCurrentProject()
  local t = p and p:GetCurrentTimeline()
  return { resolve = resolve:GetVersionString(), page = resolve:GetCurrentPage(), project = p and p:GetName(),
           timeline = t and t:GetName(), timeline_fps = t and t:GetSetting("timelineFrameRate"),
           timeline_start_frame = t and t:GetStartFrame(), timeline_end_frame = t and t:GetEndFrame(),
           current_timecode = t and t:GetCurrentTimecode() }
end

function OPS.list_timelines()
  local p, out = proj(), {}
  for i = 1, p:GetTimelineCount() do out[#out + 1] = p:GetTimelineByIndex(i):GetName() end
  return out
end

function OPS.set_current_timeline(a)
  local p = proj()
  for i = 1, p:GetTimelineCount() do
    local t = p:GetTimelineByIndex(i)
    if t:GetName() == a.name then return { ok = p:SetCurrentTimeline(t) and true or false } end
  end
  error("timeline not found: " .. tostring(a.name), 0)
end

function OPS.list_media()
  local out = {}
  for _, c in ipairs(all_clips()) do out[#out + 1] = clip_info(c) end
  return out
end

function OPS.import_media(a)
  local out = {}
  for _, c in ipairs(each(resolve:GetMediaStorage():AddItemListToMediaPool(a.paths))) do out[#out + 1] = clip_info(c) end
  return out
end

function OPS.create_timeline(a)
  local t
  if a.clips and #a.clips > 0 then
    local cs = {}
    for _, r in ipairs(a.clips) do cs[#cs + 1] = find_clip(r) end
    t = pool():CreateTimelineFromClips(a.name, cs)
  else
    t = pool():CreateEmptyTimeline(a.name)
  end
  if not t then error("Resolve refused to create the timeline (name already used?)", 0) end
  return { name = t:GetName() }
end

function OPS.append_clips(a)
  if type(a.clips) ~= "table" or #a.clips == 0 then
    error("clips must be a non-empty array", 0)
  end
  local list = {}
  for index, spec in ipairs(a.clips) do
    if type(spec) ~= "table" or type(spec.clip) ~= "string" or spec.clip == "" then
      error("clips[" .. index .. "].clip must be a non-empty media id or exact name", 0)
    end
    local c = find_clip(spec.clip)
    local props = c:GetClipProperty() or {}
    local total_frames = tonumber(props["Frames"])
    local first = tonumber(spec.start_frame or 0)
    local finish = tonumber(spec.end_frame)
    if first == nil or first < 0 or first ~= math.floor(first) then
      error("clips[" .. index .. "].start_frame must be a non-negative integer", 0)
    end
    if finish == nil then
      if not total_frames or total_frames < 1 then
        error("cannot infer source duration for " .. spec.clip .. "; provide end_frame", 0)
      end
      finish = math.floor(total_frames)
    end
    if finish <= first or finish ~= math.floor(finish) then
      error("clips[" .. index .. "].end_frame is an exclusive bound and must be an integer > start_frame", 0)
    end
    if total_frames and finish > total_frames then
      error("clips[" .. index .. "] exclusive end_frame exceeds source duration (" .. total_frames .. " frames)", 0)
    end
    local d = { mediaPoolItem = c, startFrame = first, endFrame = finish }
    if spec.track_index ~= nil then
      local track = tonumber(spec.track_index)
      if not track or track < 1 or track ~= math.floor(track) then
        error("clips[" .. index .. "].track_index must be a positive integer", 0)
      end
      d.trackIndex = track
    end
    if spec.record_frame ~= nil then
      local record = tonumber(spec.record_frame)
      if not record or record < 0 or record ~= math.floor(record) then
        error("clips[" .. index .. "].record_frame must be a non-negative integer", 0)
      end
      d.recordFrame = record
    end
    if spec.media_type ~= nil then
      local media_type = tonumber(spec.media_type)
      if media_type ~= 1 and media_type ~= 2 then
        error("clips[" .. index .. "].media_type must be 1 (video only) or 2 (audio only)", 0)
      end
      d.mediaType = media_type
    end
    list[#list + 1] = d
  end
  local appended = pool():AppendToTimeline(list)
  if not appended then error("Resolve AppendToTimeline returned no result", 0) end
  local out = {}
  for _, it in ipairs(each(appended)) do out[#out + 1] = item_info(it) end
  return out
end

function OPS.duplicate_timeline(a)
  local t = tl():DuplicateTimeline(a.name)
  if not t then error("Resolve refused to duplicate the timeline (name already used?)", 0) end
  return { name = t:GetName() }
end

function OPS.add_track(a)
  local kind = a.track_type or "audio"
  if kind ~= "audio" and kind ~= "video" then error("track_type must be audio or video", 0) end
  local t = tl()
  local ok
  if kind == "audio" then
    ok = t:AddTrack("audio", "stereo")
  else
    ok = t:AddTrack("video")
  end
  return { ok = ok and true or false, count = t:GetTrackCount(kind) }
end

-- Primary grade on node 1 via ASC CDL. slope/offset/power are "r g b" strings, saturation a number string.
-- Which colour-page calls does this Resolve expose to scripts? (reports, changes nothing)
function OPS.probe_color(a)
  local it = find_item(a.item_id)
  local out = { item = {}, graph = {} }
  for _, k in ipairs({ "GetNumNodes", "SetLUT", "GetLUT", "SetCDL", "GetNodeGraph", "AddNode", "AddVersion",
                       "GetColorGroup", "CopyGrades", "ApplyGradeFromDRX", "SetClipColor" }) do
    out.item[k] = type(it[k])
  end
  local okg, g = pcall(function() return it:GetNodeGraph() end)
  if okg and g then
    for _, k in ipairs({ "GetNumNodes", "SetLUT", "GetLUT", "SetNodeEnabled", "GetNodeLabel", "ApplyGradeFromDRX",
                         "ApplyArriCdlLut", "ResetAllGrades", "AddNode", "AddSerialNode", "GetToolsInNode" }) do
      out.graph[k] = type(g[k])
    end
    local okn, n = pcall(function() return g:GetNumNodes() end)
    out.graph_nodes = okn and n or nil
  end
  local okn, n = pcall(function() return it:GetNumNodes() end)
  out.item_nodes = okn and n or nil
  return out
end

function OPS.refresh_luts() return { ok = proj():RefreshLUTList() and true or false } end

-- Load a .cube LUT onto node `node` (default 1) of each item.
function OPS.set_lut(a)
  local out = {}
  for _, id in ipairs(a.item_ids) do
    out[id] = find_item(id):SetLUT(a.node or 1, a.path) and true or false
  end
  return out
end

function OPS.set_grade(a)
  local cdl = { NodeIndex = tostring(a.node or 1), Slope = a.slope or "1 1 1", Offset = a.offset or "0 0 0",
                Power = a.power or "1 1 1", Saturation = tostring(a.saturation or 1) }
  local out = {}
  for _, id in ipairs(a.item_ids) do out[id] = find_item(id):SetCDL(cdl) and true or false end
  return out
end

function OPS.list_timeline_items(a)
  local out = {}
  for _, x in ipairs(items(a.track_type or "video")) do out[#out + 1] = item_info(x[1], x[2], x[3]) end
  return out
end

function OPS.set_item_properties(a)
  for k in pairs(a.properties) do
    if not PROPS[k] then error("property not allowed: " .. k, 0) end
  end
  local it, out = find_item(a.item_id), {}
  for k, v in pairs(a.properties) do out[k] = it:SetProperty(k, v) and true or false end
  return out
end

function OPS.delete_items(a)
  local list = {}
  for _, id in ipairs(a.item_ids) do list[#list + 1] = find_item(id) end
  return { ok = tl():DeleteClips(list, a.ripple or false) and true or false }
end

local function text_tool(it)
  local comp = it:GetFusionCompByIndex(1)
  local tool = comp and comp:FindToolByID("TextPlus")
  if not tool then
    for _, x in pairs(comp and comp:GetToolList(false) or {}) do
      if x.ID == "TextPlus" then tool = x end
    end
  end
  if not tool then error("no Text+ tool in this title", 0) end
  return tool
end

-- Font/size/position plus other Text+ inputs by id. Enabled* first: an element's inputs don't exist until it is on.
local function apply_text_style(tool, a)
  if a.font then tool:SetInput("Font", a.font) end
  if a.style then tool:SetInput("Style", a.style) end
  if a.size then tool:SetInput("Size", a.size) end
  if a.y then tool:SetInput("Center", { a.x or 0.5, a.y }) end
  local keys = {}
  for k, v in pairs(a.inputs or {}) do
    if type(k) == "string" and k:match("^%a+%d?$") and type(v) == "number" then keys[#keys + 1] = k end
  end
  table.sort(keys, function(p, q)
    local ep, eq = p:match("^Enabled") ~= nil, q:match("^Enabled") ~= nil
    if ep ~= eq then return ep end
    return p < q
  end)
  for _, k in ipairs(keys) do tool:SetInput(k, a.inputs[k]) end
end

-- Restyle existing Text+ titles without replacing them.
function OPS.style_text_plus(a)
  local n = 0
  for _, id in ipairs(a.item_ids) do apply_text_style(text_tool(find_item(id)), a); n = n + 1 end
  return { styled = n }
end

-- Editable Text+ titles. Each spec: {text, record_frame, frames}. Style is optional (font, style, size, x, y).
-- Reports which length/track calls this Resolve exposes, because they vary by version.
function OPS.add_text_plus(a)
  local t = tl()
  local fps = math.floor(tonumber(t:GetSetting("timelineFrameRate")) or 30)
  local function tc(f)
    return string.format("%02d:%02d:%02d:%02d", math.floor(f / (fps * 3600)), math.floor(f / (fps * 60)) % 60,
                         math.floor(f / fps) % 60, f % fps)
  end
  local report = { timeline = {}, item = {} }
  for _, k in ipairs({ "InsertFusionTitleIntoTimeline", "InsertTitleIntoTimeline", "SetTrackLock", "GetIsTrackLocked",
                       "SetTrackEnable" }) do report.timeline[k] = type(t[k]) end
  local locked = {}
  for _, n in ipairs(a.lock_video_tracks or {}) do
    if t:SetTrackLock("video", n, true) then locked[#locked + 1] = n end
  end
  local out = {}
  local ok, err = pcall(function()
    for _, spec in ipairs(a.items) do
      if not t:SetCurrentTimecode(tc(spec.record_frame)) then error("could not move playhead to " .. tc(spec.record_frame), 0) end
      local it = t:InsertFusionTitleIntoTimeline("Text+")
      if not it then error("Resolve refused to insert Text+", 0) end
      for _, k in ipairs({ "SetStart", "SetEnd", "SetDuration", "SetLeftOffset", "SetRightOffset", "GetFusionCompByIndex" }) do
        report.item[k] = type(it[k])
      end
      local tool = text_tool(it)
      tool:SetInput("StyledText", spec.text)
      apply_text_style(tool, a)
      if a.list_inputs then
        report.inputs = {}
        for _, inp in pairs(tool:GetInputList()) do report.inputs[#report.inputs + 1] = inp:GetAttrs().INPS_ID end
      end
      local trimmed = "none"
      if spec.frames then
        if type(it.SetEnd) == "function" and it:SetEnd(spec.record_frame + spec.frames) then trimmed = "SetEnd"
        elseif type(it.SetDuration) == "function" and it:SetDuration(spec.frames) then trimmed = "SetDuration" end
      end
      local info = item_info(it, "video")
      info.trimmed = trimmed
      out[#out + 1] = info
    end
  end)
  for _, n in ipairs(locked) do t:SetTrackLock("video", n, false) end
  if not ok then error(err, 0) end
  report.items = out
  return report
end

function OPS.add_marker(a)
  return { ok = tl():AddMarker(a.frame, a.color or "Blue", a.name or "", a.note or "", a.duration or 1, "") and true or false }
end

function OPS.list_markers() return tl():GetMarkers() or {} end

function OPS.delete_marker(a) return { ok = tl():DeleteMarkerAtFrame(a.frame) and true or false } end

function OPS.goto_timecode(a) return { ok = tl():SetCurrentTimecode(a.timecode) and true or false } end

function OPS.export_frame(a)
  if a.timecode then tl():SetCurrentTimecode(a.timecode) end
  local path = DIR .. "frame.png"
  if not proj():ExportCurrentFrameAsStill(path) then
    error("ExportCurrentFrameAsStill failed (try the Edit or Color page)", 0)
  end
  return { path = path }
end

function OPS.open_page(a)
  if not PAGES[a.page] then error("unknown page: " .. tostring(a.page), 0) end
  return { ok = resolve:OpenPage(a.page) and true or false }
end

function OPS.save_project() return { ok = resolve:GetProjectManager():SaveProject() and true or false } end

function OPS.list_render_presets() return proj():GetRenderPresetList() or {} end

function OPS.queue_render(a)
  local p = proj()
  if a.preset and not p:LoadRenderPreset(a.preset) then error("render preset not found: " .. a.preset, 0) end
  if not p:SetRenderSettings({ SelectAllFrames = true, TargetDir = a.target_dir, CustomName = a.name }) then
    error("SetRenderSettings failed", 0)
  end
  local job = p:AddRenderJob()
  if not job then error("AddRenderJob failed", 0) end
  return { job_id = job }
end

function OPS.start_render(a)
  local p = proj()
  local ok
  if a.job_ids and #a.job_ids > 0 then ok = p:StartRendering(a.job_ids) else ok = p:StartRendering() end
  return { ok = ok and true or false }
end

function OPS.render_status()
  local p, jobs = proj(), {}
  for _, j in ipairs(each(p:GetRenderJobList())) do
    local st = p:GetRenderJobStatus(j.JobId) or {}
    st.job = j.JobId
    jobs[#jobs + 1] = st
  end
  return { rendering = p:IsRenderingInProgress() and true or false, jobs = jobs }
end

-- ---------- loop ----------
-- No delete/rename here: requests carry an id and we answer each id once. mcp_server.py deletes old files.
local me = tostring({}):gsub("table: ", "")  -- unique per run
fu:SetData("claude_bridge_inst", me)  -- in-memory; a newer run overwrites it and this loop exits
local function req_id(raw) return raw and raw:match('"id"%s*:%s*"([^"]*)"') end
local last = req_id(readreq())  -- ignore a stale request left from before this run
log("running. Folder: " .. DIR)

while fu:GetData("claude_bridge_inst") == me do
  local raw = readreq()
  local id = req_id(raw)
  local ok, req = false, nil
  if id and id ~= last then ok, req = pcall(json.decode, raw) end
  if ok and type(req) == "table" then
    last = id
    reply(id)  -- ack
    local good, out = pcall(function()
      local op = OPS[req.op]
      if not op then error("unknown op: " .. tostring(req.op), 0) end
      return { id = id, ok = true, result = op(req.args or {}) }
    end)
    if not good then out = { id = id, ok = false, error = tostring(out) } end
    reply(id, json.encode(out))
  else
    sleep(0.1)
  end
end
log("stopped (another copy started).")
end

local ok, err = pcall(main)
if not ok then log("FAILED: " .. tostring(err)) end
