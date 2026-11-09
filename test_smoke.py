"""Smoke test: fake `resolve` (Lua) -> real resolve_bridge.lua -> real mcp_server.py over stdio.

Needs `pip install lupa` (a Lua runtime for Python). Run: python test_smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

try:
    import lupa.luajit21 as lua_mod  # Resolve uses LuaJIT; fall back to plain Lua
except ImportError:
    import lupa as lua_mod

here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
bridge_dir = os.path.join(tmp, "resolve-claude-bridge")
os.makedirs(bridge_dir)

FAKE = r"""
local clip = {GetMediaId=function() return "id1" end, GetName=function() return "a.mp4" end,
  GetClipProperty=function() return {Frames="240", FPS="24", ["File Path"]="C:\\v\\a.mp4"} end}
local folder = {GetClipList=function() return {clip} end, GetSubFolderList=function() return {} end}
local pool = {GetRootFolder=function() return folder end,
  AppendToTimeline=function(self, list) record(list[1].startFrame, list[1].endFrame); return {} end}
local proj = {GetName=function() return "demo" end, GetCurrentTimeline=function() return nil end,
  GetMediaPool=function() return pool end}
resolve = {GetVersionString=function() return "21.1-fake" end, GetCurrentPage=function() return "edit" end,
  GetProjectManager=function() return {GetCurrentProject=function() return proj end} end}
"""

L = {}
appended = []


prefs = os.path.join(tmp, "Fusion.prefs")
data = {}


def save_prefs(*_):  # mimic Fusion: writes every SetData value into the prefs file
    with open(prefs, "w", encoding="utf-8") as f:
        f.write("{\n" + "".join(f'  {k} = "{v}",\n' for k, v in data.items() if v is not None) + "}\n")


def run_bridge():
    lua = lua_mod.LuaRuntime()
    L["lua"] = lua
    g = lua.globals()
    g.BRIDGE_DIR = bridge_dir + "\\"
    g.record = lambda s, e: appended.append((s, e))
    # mimic Resolve's sandbox: no io/require; loadfile reads, fusion prefs is the only writer
    lua.execute("io = nil; require = nil; bmd = {}; fusion = {}")
    g.bmd.wait = time.sleep
    g.fusion.SetData = lambda self, k, v: data.__setitem__(k, v)
    g.fusion.GetData = lambda self, k: data.get(k)
    g.fusion.SavePrefs = save_prefs
    lua.execute(FAKE)
    lua.execute(open(os.path.join(here, "resolve_bridge.lua"), encoding="utf-8").read())


threading.Thread(target=run_bridge, daemon=True).start()
for _ in range(50):
    if "claude_bridge_inst" in data:
        break
    time.sleep(0.1)

env = dict(os.environ, RESOLVE_BRIDGE_DIR=bridge_dir, RESOLVE_FUSION_PREFS=prefs)
p = subprocess.Popen([sys.executable, os.path.join(here, "mcp_server.py")], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, text=True, env=env)


def rpc(i, method, params=None):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())["result"]


def text(r):
    return r["content"][0]["text"]


assert rpc(1, "initialize")["serverInfo"]["name"] == "davinci-resolve"
assert len(rpc(2, "tools/list")["tools"]) >= 20
r = rpc(3, "tools/call", {"name": "get_state"})
assert not r.get("isError") and json.loads(text(r))["resolve"] == "21.1-fake", r
r = rpc(4, "tools/call", {"name": "list_media"})
media = json.loads(text(r))
assert media[0]["name"] == "a.mp4" and media[0]["path"] == "C:\\v\\a.mp4", r
r = rpc(5, "tools/call", {"name": "append_clips",
                          "arguments": {"clips": [{"clip": "a.mp4", "start_frame": 10, "end_frame": 50}]}})
assert not r.get("isError"), r
assert appended == [(10, 50)], appended
r = rpc(6, "tools/call", {"name": "set_item_properties", "arguments": {"item_id": "x", "properties": {"Evil": 1}}})
assert r["isError"] and "not allowed" in text(r), r
r = rpc(8,"tools/call", {"name": "add_marker", "arguments": {"frame": 5}})
assert r["isError"] and "No current timeline" in text(r), r

data["claude_bridge_inst"] = "other"  # a newer run took over: stops the Lua loop
time.sleep(0.5)
r = rpc(9, "tools/call", {"name": "get_state"})
assert r["isError"] and "not running" in text(r), r
p.terminate()
print("OK using", lua_mod.__name__)
