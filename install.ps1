<#
.SYNOPSIS  Sumie one-step installer for Windows.
.EXAMPLE   .\install.ps1
.EXAMPLE   .\install.ps1 -Ai                       # also PyTorch + AI models (~400 MB)
.EXAMPLE   .\install.ps1 -Game "D:\SteamLibrary\steamapps\common\Draw & Guess"
.EXAMPLE   .\install.ps1 -Check | -Uninstall
#>
param([switch]$Ai, [switch]$Check, [switch]$Uninstall, [string]$Game = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Say($m) { Write-Host "==> $m" -ForegroundColor Magenta }
function Die($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { Die "Python not found. Install it with: winget install Python.Python.3.12 (then open a new terminal)" }
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Die "Python 3.10 or newer is required." }
# the .NET SDK is checked by tools/install.py

$gameArgs = @()
if ($Game) { $gameArgs = @("--game", $Game) }

if ($Uninstall) { Say "Removing the plugin"; & python tools/install.py --uninstall @gameArgs; exit $LASTEXITCODE }
if ($Check)     { & python tools/install.py --check @gameArgs; exit $LASTEXITCODE }

Say "1/2  Setting up the Python environment (.venv inside this folder)"
if ($Ai) { & python tools/setup.py --ai } else { & python tools/setup.py }
if ($LASTEXITCODE -ne 0) { Die "setup failed" }
Say "2/2  Building and installing the plugin (close the game first)"
& python tools/install.py @gameArgs
if ($LASTEXITCODE -ne 0) { Die "install failed" }
Say "Done. Start the game from Steam, then open http://127.0.0.1:8765/"
