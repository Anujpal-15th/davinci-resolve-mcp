#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor/davinci-resolve-mcp"
COMMIT="8f9e488a1be0d1bd8be418cc4243b695781061cd"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
mkdir -p "$ROOT/vendor"
if [[ ! -d "$VENDOR/.git" ]]; then
  [[ ! -e "$VENDOR" ]] || { echo "Vendor folder exists but is not a git checkout" >&2; exit 1; }
  git clone --filter=blob:none --no-checkout https://github.com/samuelgursky/davinci-resolve-mcp.git "$VENDOR"
fi
git -C "$VENDOR" fetch --depth 1 origin "$COMMIT"
git -C "$VENDOR" checkout --detach FETCH_HEAD
(cd "$VENDOR" && "$PYTHON" install.py --clients manual)
if [[ "$(uname)" == Darwin ]]; then UTIL="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"; else UTIL="$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Utility"; fi
mkdir -p "$UTIL" && cp "$ROOT/resolve_bridge.lua" "$UTIL/"
echo "Note: the legacy bridge is only tested on Windows. Studio users should use the resolve__ tools."
cat > "$ROOT/.mcp.json" <<EOF
{
  "mcpServers": {
    "resolve-creative-editor": {
      "command": "$PYTHON",
      "args": ["$ROOT/unified_server.py"],
      "env": {
        "RESOLVE_MCP_REPO": "$VENDOR",
        "RESOLVE_MCP_PYTHON": "$VENDOR/venv/bin/python",
        "RESOLVE_MCP_SERVER": "$VENDOR/src/server.py",
        "PYTHONUTF8": "1"
      }
    }
  }
}
EOF
echo "Setup complete. Restart your MCP client."
