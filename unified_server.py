#!/usr/bin/env python3
"""Single stdio MCP gateway for the upstream Resolve MCP and the user's legacy bridge.

Tool namespaces:
  resolve__* -> upstream compound Resolve MCP
  legacy__*  -> original Lua bridge MCP
  media__*   -> original FFmpeg/transcription/contact-sheet tools (run in this process)
  edit__*    -> verified high-level editing tools built on the bridge (run in this process)
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
UPSTREAM_REPO = pathlib.Path(os.environ.get("RESOLVE_MCP_REPO", ROOT / "vendor" / "davinci-resolve-mcp")).resolve()
UPSTREAM_SERVER = pathlib.Path(os.environ.get("RESOLVE_MCP_SERVER", UPSTREAM_REPO / "src" / "server.py")).resolve()
LEGACY_SERVER = ROOT / "mcp_server.py"
PINNED_COMMIT = "8f9e488a1be0d1bd8be418cc4243b695781061cd"
# Upstream tools that work without a live scripting connection, so they stay useful on free Resolve.
OFFLINE_UPSTREAM = {"knowledge", "media_analysis", "setup"}
# Legacy tools that destroy work or start long jobs need an explicit confirm=true.
GUARDED_LEGACY = {"delete_items", "start_render"}
READ_ONLY_LEGACY = {"get_state", "list_timelines", "list_media", "list_timeline_items", "list_markers", "probe_color",
                    "export_frame", "list_render_presets", "render_status", "goto_timecode", "open_page",
                    "set_current_timeline"}
INSTRUCTIONS = """Controls DaVinci Resolve. On free Resolve use legacy__ and edit__ tools; resolve__ timeline tools need Studio.
Before editing: run edit__timeline_report, since the user may have edited by hand, and edit__snapshot_timeline.
Make small targeted changes and never rebuild a whole timeline. Ask before deleting or replacing clips.
Never render unless the user says to. Do not mix legacy__ and resolve__ edits on one timeline.
Read resolve__knowledge for editorial, colour and audio guidance. You cannot hear audio: say so rather than implying it."""

class Child:
    def __init__(self, label: str, command: list[str], cwd: pathlib.Path):
        self.label = label
        self.command = command
        self.cwd = str(cwd)
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.lock = threading.Lock()
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.reader: threading.Thread | None = None
        self.initialize_result: dict[str, Any] | None = None
        self.tools: list[dict[str, Any]] = []

    def start(self, protocol_version: str) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=os.environ.copy(),
        )
        self.reader = threading.Thread(target=self._read_responses, daemon=True, name=f"{self.label}-stdout")
        self.reader.start()
        result = self.request("initialize", {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "resolve-creative-editor-gateway", "version": "1.0.0"},
        })
        if "error" in result:
            raise RuntimeError(f"{self.label} initialize failed: {result['error']}")
        self.initialize_result = result.get("result", {})
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {})
        if "error" in listed:
            raise RuntimeError(f"{self.label} tools/list failed: {listed['error']}")
        self.tools = listed.get("result", {}).get("tools", [])

    def _read_responses(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and "id" in response:
                self.responses.put(response)

    def request(self, method: str, params: dict[str, Any], timeout: float = 180.0) -> dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError(f"{self.label} MCP process is not running")
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:  # discard late replies to requests that already timed out
                try:
                    response = self.responses.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty as exc:
                    raise TimeoutError(f"{self.label} timed out handling {method} after {timeout:g}s") from exc
                if response.get("id") == request_id:
                    return response

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
            self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


def _upstream_python() -> pathlib.Path:
    override = os.environ.get("RESOLVE_MCP_PYTHON")
    if override:
        return pathlib.Path(override).resolve()
    if os.name == "nt":
        return UPSTREAM_REPO / "venv" / "Scripts" / "python.exe"
    return UPSTREAM_REPO / "venv" / "bin" / "python"


def _command_from_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{name} must be a JSON array of command strings")
    return value


def _make_children() -> tuple[Child | None, Child]:
    legacy_cmd = _command_from_env("LEGACY_MCP_COMMAND", [sys.executable, str(LEGACY_SERVER)])
    legacy = Child("legacy bridge", legacy_cmd, ROOT)
    upstream_override = os.environ.get("RESOLVE_MCP_COMMAND")
    upstream_python = _upstream_python()
    if not upstream_override and not (UPSTREAM_SERVER.is_file() and upstream_python.is_file()):
        sys.stderr.write("upstream engine not installed; continuing with legacy__, media__ and edit__\n")
        return None, legacy
    upstream_cmd = _command_from_env("RESOLVE_MCP_COMMAND", [str(upstream_python), str(UPSTREAM_SERVER)])
    return Child("upstream Resolve", upstream_cmd, ROOT if upstream_override else UPSTREAM_REPO), legacy


def _prefix_tool(tool: dict[str, Any], prefix: str, warning: str = "") -> dict[str, Any]:
    result = dict(tool)
    original = result["name"]
    result["name"] = prefix + original
    if warning:
        result["description"] = (warning + "\n\n" + result.get("description", "")).strip()
    return result


def _media_tools() -> list[dict[str, Any]]:
    from mcp_server import TOOLS
    names = {"probe_media", "extract_audio", "detect_silence", "transcribe_audio", "make_contact_sheet"}
    return [_prefix_tool(t, "media__", "Local media analysis; source files are read-only.") for t in TOOLS if t["name"] in names]


def _edit_tools() -> list[dict[str, Any]]:
    import editor_tools
    return [_prefix_tool(t, "edit__") for t in editor_tools.TOOLS]


def _run_edit_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    import editor_tools
    try:
        result = editor_tools.run(name, args)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                "isError": result.get("_operation", {}).get("status") == "failed"}
    except Exception as exc:
        return {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}


def _with_confirm(tool: dict[str, Any]) -> dict[str, Any]:
    schema = dict(tool.get("inputSchema") or {"type": "object", "properties": {}})
    schema["properties"] = {**schema.get("properties", {}),
                            "confirm": {"type": "boolean", "description": "Must be true. Ask the user first."}}
    schema["required"] = list(schema.get("required", [])) + ["confirm"]
    return {**tool, "inputSchema": schema}


def _run_media_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from mcp_server import run_media_tool
    try:
        result = run_media_tool(name, args)
        if name == "make_contact_sheet":
            return {"content": [
                {"type": "image", "data": result["image"], "mimeType": result["mimeType"]},
                {"type": "text", "text": json.dumps({k:v for k,v in result.items() if k not in {"image","mimeType"}}, indent=2)}
            ]}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
    except Exception as exc:
        return {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}


class Gateway:
    def __init__(self):
        self.resolve: Child | None = None
        self.legacy: Child | None = None
        self.tool_routes: dict[str, tuple[str, str]] = {}
        self.started = False
        self.protocol = "2025-03-26"
        self.upstream_mode = "offline"

    def _start_child(self, child: Child | None, protocol: str) -> Child | None:
        if child is None:
            return None
        try:
            child.start(protocol)
            return child
        except Exception as exc:
            sys.stderr.write(f"{child.label} failed to start: {exc}\n")
            child.close()
            return None

    def _detect_upstream_mode(self) -> str:
        """all = every resolve__ tool; offline = only those that work without scripting. Never launches Resolve."""
        forced = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--upstream-tools=")),
                      os.environ.get("RCE_UPSTREAM_TOOLS", "auto"))
        if forced in {"all", "offline"} or not self.resolve:
            return forced if self.resolve else "offline"
        def ask(action: str) -> dict[str, Any]:
            return self.resolve.request("tools/call", {"name": "resolve_control", "arguments": {"action": action}},
                                        timeout=30.0).get("result", {})
        try:
            first = ask("runtime_mode")
            try:
                if json.loads(first["content"][0]["text"]).get("running") is False:
                    return "offline"  # get_version could launch Resolve
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            return "offline" if ask("get_version").get("isError") else "all"
        except Exception:
            return "offline"

    def start(self, protocol: str) -> None:
        if self.started:
            return
        self.protocol = protocol
        self.resolve, self.legacy = _make_children()
        self.resolve = self._start_child(self.resolve, protocol)
        self.legacy = self._start_child(self.legacy, protocol)
        self.upstream_mode = self._detect_upstream_mode()
        self.started = True

    def tools_list(self) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        self.tool_routes.clear()
        for tool in (self.resolve.tools if self.resolve else []):
            if self.upstream_mode == "offline" and tool["name"] not in OFFLINE_UPSTREAM:
                continue
            t = _prefix_tool(tool, "resolve__")
            tools.append(t)
            self.tool_routes[t["name"]] = ("resolve", tool["name"])
        for tool in (self.legacy.tools if self.legacy else []):
            if tool["name"] in {"probe_media", "extract_audio", "detect_silence", "transcribe_audio", "make_contact_sheet"}:
                continue
            t = _prefix_tool(
                tool, "legacy__",
                "FALLBACK ONLY: prefer the matching resolve__ tool. Do not mix legacy__ and resolve__ timeline mutations in one edit."
            )
            if tool["name"] in GUARDED_LEGACY:
                t = _with_confirm(t)
            tools.append(t)
            self.tool_routes[t["name"]] = ("legacy", tool["name"])
        for tool in _media_tools():
            tools.append(tool)
            self.tool_routes[tool["name"]] = ("media", tool["name"].replace("media__", ""))
        for tool in _edit_tools():
            tools.append(tool)
            self.tool_routes[tool["name"]] = ("edit", tool["name"].replace("edit__", ""))
        return {"tools": tools}

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        params = msg.get("params") or {}
        msg_id = msg.get("id")
        if method == "initialize":
            requested = params.get("protocolVersion", self.protocol)
            self.start(requested)
            upstream = self.resolve.initialize_result if self.resolve else {}
            caps = dict((upstream or {}).get("capabilities", {}))
            caps["tools"] = {}
            return {"protocolVersion": (upstream or {}).get("protocolVersion", requested),
                    "capabilities": caps,
                    "instructions": INSTRUCTIONS,
                    "serverInfo": {"name": "resolve-creative-editor", "version": "1.0.0"}}
        if method == "notifications/initialized":
            # Child servers receive their own initialized notification during startup.
            return None
        if method == "ping":
            return {}
        if not self.started:
            self.start(self.protocol)
        if method == "tools/list":
            return self.tools_list()
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            route = self.tool_routes.get(name)
            if route is None:
                # Build route table on first call if host skipped tools/list.
                self.tools_list()
                route = self.tool_routes.get(name)
            if route is None:
                return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}
            backend, original = route
            if backend == "media":
                return _run_media_tool(original, args)
            if backend == "edit":
                return _run_edit_tool(original, args)
            if backend == "legacy" and original in GUARDED_LEGACY:
                if args.get("confirm") is not True:
                    return {"isError": True, "content": [{"type": "text", "text":
                            f"Blocked: {original} needs confirm=true. Ask the user first."}]}
                args = {k: v for k, v in args.items() if k != "confirm"}
            child = self.resolve if backend == "resolve" else self.legacy
            if child is None:
                return {"isError": True, "content": [{"type": "text", "text": f"The {backend} backend is not available."}]}
            response = child.request("tools/call", {"name": original, "arguments": args}, timeout=1800.0)
            if "error" in response:
                return {"isError": True, "content": [{"type": "text", "text": str(response["error"])}]}
            result = response.get("result", {})
            if backend == "legacy" and original not in READ_ONLY_LEGACY:
                import editor_tools
                editor_tools.log_audit(name, args, "failed" if result.get("isError") else "success")
            return result
        if method in {"resources/list", "resources/read", "prompts/list", "prompts/get"}:
            if not self.resolve:
                if method.endswith("/list"):
                    return {method.split("/")[0]: []}
                return {"_rpc_error": {"code": -32601, "message": "upstream not installed"}}
            response = self.resolve.request(method, params)
            if "error" in response:
                return {"_rpc_error": response["error"]}
            return response.get("result", {})
        raise LookupError(method)

    def close(self):
        if self.resolve: self.resolve.close()
        if self.legacy: self.legacy.close()


def main() -> None:
    if "--doctor" in sys.argv:
        py = _upstream_python()
        import editor_tools
        print(json.dumps({
            **editor_tools.doctor(),
            "upstream_repo": str(UPSTREAM_REPO),
            "upstream_server_exists": UPSTREAM_SERVER.is_file(),
            "upstream_python": str(py),
            "upstream_python_exists": py.is_file(),
            "legacy_server_exists": LEGACY_SERVER.is_file(),
            "pinned_commit": PINNED_COMMIT,
        }, indent=2))
        return

    gateway = Gateway()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if "id" not in message:
                    gateway.handle(message)
                    continue
                try:
                    result = gateway.handle(message)
                    if result is None:
                        continue
                    if "_rpc_error" in result:
                        output = {"jsonrpc": "2.0", "id": message["id"], "error": result["_rpc_error"]}
                    else:
                        output = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                except LookupError as exc:
                    output = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": str(exc)}}
                except Exception as exc:
                    output = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}
                sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception as exc:
                sys.stderr.write(f"gateway error: {type(exc).__name__}: {exc}\n")
                sys.stderr.flush()
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
