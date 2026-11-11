#!/usr/bin/env python3
"""Single stdio MCP gateway for the upstream Resolve MCP and the user's legacy bridge.

Tool namespaces:
  resolve__* -> upstream compound Resolve MCP
  legacy__*  -> original Lua bridge MCP
  media__*   -> original FFmpeg/transcription/contact-sheet tools (run in this process)
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
UPSTREAM_REPO = pathlib.Path(os.environ.get("RESOLVE_MCP_REPO", ROOT / "vendor" / "davinci-resolve-mcp")).resolve()
UPSTREAM_SERVER = pathlib.Path(os.environ.get("RESOLVE_MCP_SERVER", UPSTREAM_REPO / "src" / "server.py")).resolve()
LEGACY_SERVER = ROOT / "mcp_server.py"
PINNED_COMMIT = "8f9e488a1be0d1bd8be418cc4243b695781061cd"

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
            try:
                response = self.responses.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"{self.label} timed out handling {method} after {timeout:g}s") from exc
            if response.get("id") != request_id:
                raise RuntimeError(f"{self.label} returned an unexpected response id")
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


def _make_children() -> tuple[Child, Child]:
    upstream_override = os.environ.get("RESOLVE_MCP_COMMAND")
    legacy_override = os.environ.get("LEGACY_MCP_COMMAND")
    upstream_python = _upstream_python()
    if not upstream_override:
        if not UPSTREAM_SERVER.is_file():
            raise FileNotFoundError(f"Upstream server not found: {UPSTREAM_SERVER}. Run setup.ps1 first.")
        if not upstream_python.is_file():
            raise FileNotFoundError(f"Upstream venv Python not found: {upstream_python}. Run setup.ps1 first.")
    upstream_cmd = _command_from_env("RESOLVE_MCP_COMMAND", [str(upstream_python), str(UPSTREAM_SERVER)])
    legacy_cmd = _command_from_env("LEGACY_MCP_COMMAND", [sys.executable, str(LEGACY_SERVER)])
    upstream_cwd = ROOT if upstream_override else UPSTREAM_REPO
    return Child("upstream Resolve", upstream_cmd, upstream_cwd), Child("legacy bridge", legacy_cmd, ROOT)


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

    def start(self, protocol: str) -> None:
        if self.started:
            return
        self.protocol = protocol
        self.resolve, self.legacy = _make_children()
        self.resolve.start(protocol)
        self.legacy.start(protocol)
        self.started = True

    def tools_list(self) -> dict[str, Any]:
        assert self.resolve and self.legacy
        tools: list[dict[str, Any]] = []
        self.tool_routes.clear()
        for tool in self.resolve.tools:
            t = _prefix_tool(tool, "resolve__")
            tools.append(t)
            self.tool_routes[t["name"]] = ("resolve", tool["name"])
        for tool in self.legacy.tools:
            if tool["name"] in {"probe_media", "extract_audio", "detect_silence", "transcribe_audio", "make_contact_sheet"}:
                continue
            t = _prefix_tool(
                tool, "legacy__",
                "FALLBACK ONLY: prefer the matching resolve__ tool. Do not mix legacy__ and resolve__ timeline mutations in one edit."
            )
            tools.append(t)
            self.tool_routes[t["name"]] = ("legacy", tool["name"])
        for tool in _media_tools():
            tools.append(tool)
            self.tool_routes[tool["name"]] = ("media", tool["name"].replace("media__", ""))
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
            child = self.resolve if backend == "resolve" else self.legacy
            response = child.request("tools/call", {"name": original, "arguments": args}, timeout=1800.0)  # type: ignore[union-attr]
            if "error" in response:
                return {"isError": True, "content": [{"type": "text", "text": str(response["error"])}]}
            return response.get("result", {})
        if method in {"resources/list", "resources/read", "prompts/list", "prompts/get"}:
            assert self.resolve
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
        print(json.dumps({
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
