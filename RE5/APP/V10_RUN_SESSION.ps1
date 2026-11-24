param([ValidateRange(15,480)][int]$Minutes=480)
if ($Minutes % 15 -ne 0) { throw "Duration must be 15-minute increments" }
$ErrorActionPreference='Stop'
$env:ETH_V42_MIN_CUSHION_PCT='0.03'
$env:ETH_V42_KILL_SWITCH='0'
$env:KALSHI_ENABLE_LIVE=''
$env:KALSHI_FEE_CEILING_VERIFIED=''
Set-Location -LiteralPath $PSScriptRoot
$PythonCommand = if(Get-Command py -ErrorAction SilentlyContinue){'py'} elseif(Get-Command python -ErrorAction SilentlyContinue){'python'} else { throw 'Python unavailable' }
Write-Host 'V11_V7D_PAPER_ONLY | NO LIVE ORDERS | $1000 SIMULATED CAP'
Write-Host ('SESSION_FOLDER='+$PSScriptRoot)
& $PythonCommand -u '.\V7C_TEST.py'
if($LASTEXITCODE -ne 0){throw 'Settlement tests failed: collector NOT started'}
& $PythonCommand -u '.\ETH_V42_2_BIGBET_1000_PREFLIGHT.py'
if($LASTEXITCODE -ne 0){throw 'Preflight failed: collector NOT started'}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File '.\ETH_V42_SUPERVISOR.ps1' -Minutes $Minutes
if($LASTEXITCODE -ne 0){throw 'Supervisor failed: inspect session logs'}
