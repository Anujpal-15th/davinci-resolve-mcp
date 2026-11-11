# Resolve Creative Editor setup for Windows.
#   .\setup.ps1                     Full install, registered with Claude Code
#   .\setup.ps1 -Studio             Always expose all upstream tools
#   .\setup.ps1 -Lite               Skip the upstream engine (free Resolve only needs legacy__ and edit__)
#   .\setup.ps1 -WithTranscription  Also install faster-whisper
#   .\setup.ps1 -NoRegister         Write a local .mcp.json instead of registering
param([switch]$Studio, [switch]$Lite, [switch]$WithTranscription, [switch]$NoRegister)

# Windows PowerShell 5.1 turns native stderr into fatal errors under "Stop", so exit codes are checked by hand.
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"  # the upstream installer prints Unicode
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor\davinci-resolve-mcp"
$Commit = "8f9e488a1be0d1bd8be418cc4243b695781061cd"
$Gateway = Join-Path $Root "unified_server.py"
function Fail($message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 1 }

Write-Host "== Resolve Creative Editor setup ==" -ForegroundColor Cyan

$Python = $null
foreach ($candidate in @(@("py", "-3.12"), @("py", "-3"), @("python"))) {
  if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
  $extra = @($candidate | Select-Object -Skip 1)
  $found = & $candidate[0] @extra -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
  if ($LASTEXITCODE -eq 0 -and $found) { $Python = "$found".Trim(); break }
}
if (-not $Python) { Fail "Python 3.10 or newer is required: https://www.python.org/downloads/" }
Write-Host "Python: $Python"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "WARNING: FFmpeg not found. media__ tools and audio markers need it." -ForegroundColor Yellow
}

if ($Lite) {
  Write-Host "Lite install: skipping the upstream engine."
} elseif (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "WARNING: Git not found, skipping the upstream engine." -ForegroundColor Yellow
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path $Vendor) | Out-Null
  $ok = $true
  if (-not (Test-Path (Join-Path $Vendor ".git"))) {
    if (Test-Path $Vendor) { Fail "Vendor folder exists but is not a Git checkout: $Vendor. Move it aside and rerun." }
    git clone --filter=blob:none --no-checkout https://github.com/samuelgursky/davinci-resolve-mcp.git $Vendor
    $ok = $LASTEXITCODE -eq 0
  }
  if ($ok) { git -C $Vendor fetch --depth 1 origin $Commit; $ok = $LASTEXITCODE -eq 0 }
  if ($ok) { git -C $Vendor checkout --detach FETCH_HEAD; $ok = $LASTEXITCODE -eq 0 }
  if ($ok) {
    Write-Host "Installing the upstream engine (a few minutes)..."
    Push-Location $Vendor
    & $Python install.py --clients manual | Out-Null
    Pop-Location
    # The installer also exits 1 when it can't reach Resolve (closed, or free edition), so test the result instead.
    $VenvPython = Join-Path $Vendor "venv\Scripts\python.exe"
    $ok = Test-Path $VenvPython
    if ($ok) { & $VenvPython -c "import mcp" 2>$null; $ok = $LASTEXITCODE -eq 0 }
  }
  if (-not $ok) { Write-Host "WARNING: upstream engine failed to install. legacy__, media__ and edit__ still work." -ForegroundColor Yellow }
}

if ($WithTranscription) {
  & $Python -m pip install faster-whisper
  if ($LASTEXITCODE -ne 0) { Write-Host "WARNING: faster-whisper failed to install." -ForegroundColor Yellow }
}

$Utility = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
New-Item -ItemType Directory -Force -Path $Utility | Out-Null
$BridgeTarget = Join-Path $Utility "resolve_bridge.lua"
if (Test-Path $BridgeTarget) {
  $Backup = "$BridgeTarget.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Copy-Item $BridgeTarget $Backup -ErrorAction Stop
  Write-Host "Existing Lua bridge backed up to $Backup"
}
Copy-Item (Join-Path $Root "resolve_bridge.lua") $BridgeTarget -Force -ErrorAction Stop
Write-Host "Lua bridge installed."

$ServerArgs = @("-X", "utf8", $Gateway)
if ($Studio) { $ServerArgs += "--upstream-tools=all" }

$Registered = $false
if (-not $NoRegister -and (Get-Command claude -ErrorAction SilentlyContinue)) {
  claude mcp remove resolve-creative-editor -s user 2>$null | Out-Null
  claude mcp add --scope user resolve-creative-editor -- $Python @ServerArgs
  $Registered = $LASTEXITCODE -eq 0
}
if (-not $Registered) {
  $Config = @{ mcpServers = @{ "resolve-creative-editor" = @{ command = $Python; args = $ServerArgs } } }
  $ConfigPath = Join-Path $Root ".mcp.json"
  [System.IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json -Depth 8), ([System.Text.UTF8Encoding]::new($false)))
  Write-Host "Not registered automatically. Config written to $ConfigPath"
}

Write-Host ""
Write-Host "== Doctor ==" -ForegroundColor Cyan
& $Python -X utf8 $Gateway --doctor

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: restart Claude. On free Resolve, start the bridge from the Console (see README step 3)."
