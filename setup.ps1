$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor\davinci-resolve-mcp"
$Commit = "8f9e488a1be0d1bd8be418cc4243b695781061cd"

Write-Host "== Resolve Creative Editor setup ==" -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "Git is required. Install Git for Windows, reopen PowerShell, and rerun setup.ps1."
}
$Python = (Get-Command py -ErrorAction SilentlyContinue)
if ($Python) {
  $ErrorActionPreference = "Continue"; & py -3.12 --version 2>$null | Out-Null; $ErrorActionPreference = "Stop"  # PS 5.1: stderr + Stop is fatal
  if ($LASTEXITCODE -eq 0) { $PyCmd = "py"; $PyArgs = @("-3.12") }
  else { $PyCmd = "py"; $PyArgs = @("-3") }
} else {
  $Python = Get-Command python -ErrorAction SilentlyContinue
  if (-not $Python) { throw "Python 3.10+ is required." }
  $PyCmd = "python"; $PyArgs = @()
}
$Version = & $PyCmd @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
$Parts = $Version.Split('.')
if ([int]$Parts[0] -ne 3 -or [int]$Parts[1] -lt 10) { throw "Python 3.10+ required; found $Version." }

New-Item -ItemType Directory -Force -Path (Split-Path $Vendor) | Out-Null
if (-not (Test-Path (Join-Path $Vendor ".git"))) {
  if (Test-Path $Vendor) { throw "Vendor folder exists but is not a Git checkout: $Vendor. Move it aside and rerun." }
  git clone --filter=blob:none --no-checkout https://github.com/samuelgursky/davinci-resolve-mcp.git $Vendor
  if ($LASTEXITCODE -ne 0) { throw "Could not clone the upstream repository." }
}
git -C $Vendor fetch --depth 1 origin $Commit
if ($LASTEXITCODE -ne 0) { throw "Could not fetch the pinned upstream revision." }
git -C $Vendor checkout --detach FETCH_HEAD
if ($LASTEXITCODE -ne 0) { throw "Could not check out the pinned upstream revision." }

Write-Host "Installing upstream Python dependencies (this may take a few minutes)..."
Push-Location $Vendor
try {
  & $PyCmd @PyArgs install.py --clients manual
  if ($LASTEXITCODE -ne 0) { throw "Upstream installer failed. Review its diagnostic output." }
} finally { Pop-Location }

$Utility = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
New-Item -ItemType Directory -Force -Path $Utility | Out-Null
$BridgeTarget = Join-Path $Utility "resolve_bridge.lua"
if (Test-Path $BridgeTarget) {
  $Backup = "$BridgeTarget.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Copy-Item $BridgeTarget $Backup
  Write-Host "Existing Lua bridge backed up to $Backup"
}
Copy-Item (Join-Path $Root "resolve_bridge.lua") $BridgeTarget -Force

$Gateway = Join-Path $Root "unified_server.py"
$Config = @{
  mcpServers = @{
    "resolve-creative-editor" = @{
      command = $PyCmd
      args = @($PyArgs + @($Gateway))
      env = @{
        RESOLVE_MCP_REPO = $Vendor
        RESOLVE_MCP_PYTHON = (Join-Path $Vendor "venv\Scripts\python.exe")
        RESOLVE_MCP_SERVER = (Join-Path $Vendor "src\server.py")
        RESOLVE_BRIDGE_DIR = (Join-Path $env:USERPROFILE "resolve-claude-bridge")
        PYTHONUTF8 = "1"
      }
    }
  }
}
$ConfigPath = Join-Path $Root ".mcp.json"
[System.IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json -Depth 8), ([System.Text.UTF8Encoding]::new($false)))

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "One MCP entry written to: $ConfigPath"
Write-Host "Next: restart Claude Code. Open Resolve and load the Lua bridge only if legacy__ fallback is needed."
Write-Host "Run: $PyCmd $($PyArgs -join ' ') `"$Gateway`" --doctor"
