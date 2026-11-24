# ETH V42.2 BIGBET 1000 resilient collection supervisor. PAPER ONLY. PowerShell 5.1+.
# No modifications to V42 trading decisions. Old sessions archived once, never deleted.
param(
    [ValidateRange(3,1440)][int]$Minutes = 15,
    [switch]$SimulateRestart
)
$ErrorActionPreference = 'Stop'
$folder = $PSScriptRoot
$scriptName = 'ETH_V42_Collector.py'
$names = @('eth_v42_market_quotes.csv','eth_v42_shadow_candidates.csv','eth_v42_market_outcomes.csv','eth_v42_errors.csv','eth_v42_all_market_settlements.csv','eth_v42_policy_comparison.csv','eth_v42_early_shadow_120_105.csv','eth_v42_quote_disappearance.csv','eth_v42_history_readiness.csv')
$quotes = Join-Path $folder $names[0]
$bot = $null
$supervisorLog = $null
$reason = 'END_TIME_REACHED'
$restartCount = 0
$startupConfirmed = $false
$recoveryConfirmed = $false
$previousBytes = 0L
$growthChecks = 0
$lastGrowth = Get-Date
$lastReport = (Get-Date).AddMinutes(-2)
$start = Get-Date
$end = $start.AddMinutes($Minutes)
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$sessionDir = Join-Path $folder "session_logs\ETH_V7D_${Minutes}min_$stamp"
$archive = Join-Path $folder "research_archive\ETH_V7D_before_session_$stamp"
$simulated = $false
$restartTimes = New-Object 'System.Collections.Generic.List[datetime]'

function Log([string]$message, [string]$level = 'INFO') {
    $line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $message)
    Write-Host $line
    if ($supervisorLog) { Add-Content -LiteralPath $supervisorLog -Value $line -Encoding UTF8 }
}
function Stop-Collector {
    if ($null -eq $script:bot) { return }
    try {
        $script:bot.Refresh()
        if (-not $script:bot.HasExited) {
            & taskkill.exe /PID $script:bot.Id /T /F 2>$null | Out-Null
            Start-Sleep -Seconds 2
        }
    } catch { Log "Stop attempt: $($_.Exception.Message)" 'WARN' }
    $script:bot = $null
}
function Start-Collector {
    $runNumber = $script:restartCount + 1
    $stdout = Join-Path $script:sessionDir ("python_run_{0:D2}_stdout.log" -f $runNumber)
    $stderr = Join-Path $script:sessionDir ("python_run_{0:D2}_stderr.log" -f $runNumber)
    $script:bot = Start-Process -FilePath $script:python -ArgumentList @('-u',$scriptName,'run') -WorkingDirectory $folder -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $script:previousBytes = if (Test-Path -LiteralPath $quotes) { (Get-Item -LiteralPath $quotes).Length } else { 0L }
    $script:lastGrowth = Get-Date
    $script:growthChecks = 0
    Log "COLLECTOR_START PID=$($script:bot.Id) run=$runNumber baseline_bytes=$($script:previousBytes) stdout=$stdout stderr=$stderr"
}
function Restart-Collector([string]$why) {
    Log "RESTART_REQUEST reason=$why" 'WARN'
    Stop-Collector
    $script:restartTimes.Add((Get-Date))
    $recentCount = @($script:restartTimes | Where-Object { $_ -gt (Get-Date).AddHours(-1) }).Count
    if ($recentCount -gt 6) { throw 'More than six restarts within one hour. Stopping to avoid an endless failure loop.' }
    $script:restartCount++
    $delay = [math]::Min(120, 10 * [math]::Pow(2, [math]::Min(4,$recentCount-1)))
    Log "RESTART_BACKOFF seconds=$delay; existing CSVs preserved"
    $remaining = [math]::Max(0,($script:end-(Get-Date)).TotalSeconds)
    Start-Sleep -Seconds ([int][math]::Min($delay,$remaining))
    if ((Get-Date) -lt $script:end) { Start-Collector }
}
try {
    Set-Location -LiteralPath $folder
    if (-not (Test-Path -LiteralPath (Join-Path $folder $scriptName))) { throw "Missing $scriptName" }
    if (-not (Test-Path -LiteralPath (Join-Path $folder 'kalshi_eth_bot_v37.py'))) { throw 'Missing kalshi_eth_bot_v37.py helper' }
    if ([string]::IsNullOrWhiteSpace($env:KALSHI_API_KEY_ID)) { throw 'KALSHI_API_KEY_ID missing in current PowerShell session' }
    if ([string]::IsNullOrWhiteSpace($env:KALSHI_PRIVATE_KEY_PATH) -or -not (Test-Path -LiteralPath $env:KALSHI_PRIVATE_KEY_PATH)) { throw 'KALSHI_PRIVATE_KEY_PATH missing or invalid' }
    $duplicates = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|py)(\.exe)?$' -and $_.CommandLine -like "*$scriptName*" })
    if ($duplicates.Count -gt 0) { throw "Another V42 collector is active: $($duplicates.ProcessId -join ',')" }
    $pythonLine = @(& py -c 'import sys; print(sys.executable)') | Select-Object -First 1
    $script:python = [string]$pythonLine
    $script:python = $script:python.Trim()
    if (-not (Test-Path -LiteralPath $script:python)) { throw 'Python executable not found' }

    New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
    New-Item -ItemType Directory -Path $archive -Force | Out-Null
    $supervisorLog = Join-Path $sessionDir 'supervisor_events.log'
    Log "SESSION_START duration_minutes=$Minutes planned_end=$end shadow_only=true"
    foreach ($name in $names) {
        $p = Join-Path $folder $name
        if (Test-Path -LiteralPath $p) { Move-Item -LiteralPath $p -Destination (Join-Path $archive $name) -ErrorAction Stop }
    }
    Log "Previous V42 CSVs archived (not deleted): $archive"
    Start-Collector
    $lastReport = (Get-Date).AddMinutes(-2)
    while ((Get-Date) -lt $end) {
        if(Test-Path -LiteralPath (Join-Path $folder "STOP_REQUESTED.flag")) { $reason="USER_STOP_REQUESTED"; break }
        Start-Sleep -Seconds 10
        if(Test-Path -LiteralPath (Join-Path $folder "STOP_REQUESTED.flag")) { $reason="USER_STOP_REQUESTED"; break }
        $now = Get-Date
        $bot.Refresh()
        if ($bot.HasExited) {
            Restart-Collector "Python process exited, code=$($bot.ExitCode)"
            continue
        }
        $bytes = if (Test-Path -LiteralPath $quotes) { (Get-Item -LiteralPath $quotes).Length } else { 0L }
        if ($bytes -gt $previousBytes) {
            $lastGrowth = $now
            $growthChecks++
            if (-not $startupConfirmed -and $growthChecks -ge 3 -and $bytes -gt 500) {
                $startupConfirmed = $true
                Log "STARTUP_PASSED rows are being appended; bytes=$bytes"
            }
            if ($restartCount -gt 0 -and -not $recoveryConfirmed) {
                $recoveryConfirmed = $true
                Log "RECOVERY_VERIFIED bytes growing after restart; bytes=$bytes"
            }
        }
        $previousBytes = $bytes
        if (($now-$lastReport).TotalSeconds -ge 60) {
            Log ("STATUS elapsed={0:N1}m/{1}m bytes={2} no_growth={3:N0}s restarts={4} process_id={5}" -f ($now-$start).TotalMinutes,$Minutes,$bytes,($now-$lastGrowth).TotalSeconds,$restartCount,$bot.Id)
            $lastReport = $now
        }
        if ($SimulateRestart -and -not $simulated -and ($now-$start).TotalSeconds -ge 120) {
            $simulated = $true
            Restart-Collector 'DELIBERATE_TEST_PROCESS_RESTART (not an internet outage)'
            continue
        }
        if (($now-$lastGrowth).TotalSeconds -ge 120) {
            Restart-Collector 'NO_NEW_CSV_BYTES_FOR_120_SECONDS'
        } elseif (($now-$lastGrowth).TotalSeconds -ge 75 -and ($now-$lastGrowth).TotalSeconds -lt 86) {
            Log 'WARNING: No new quote bytes for 75 seconds; will restart at 120 seconds if still stalled' 'WARN'
        }
    }
} catch {
    $reason = "FAILED: $($_.Exception.Message)"
    Log $reason 'ERROR'
} finally {
    Stop-Collector
    Log "SESSION_END reason=$reason duration_minutes=$([math]::Round(((Get-Date)-$start).TotalMinutes,1)) restart_count=$restartCount"
    foreach ($name in $names) {
        $path = Join-Path $folder $name
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path
            Log "CSV $name bytes=$($item.Length) last_write=$($item.LastWriteTime)"
        } else { Log "CSV_MISSING $name" 'WARN' }
    }
    # Generate session-specific reports after collector has stopped.
    try {
        & $script:python -u (Join-Path $folder 'ETH_V42_Collector.py') compare 2>&1 | ForEach-Object { Log "REPORT_COMPARE $_" }
        & $script:python -u (Join-Path $folder 'ETH_V42_Analyze.py') --slippage-cents 0.1 2>&1 | ForEach-Object { Log "REPORT_ANALYZE $_" }
        foreach ($reportName in @('ETH_V42_report.html','ETH_V42_report.json','ETH_V42_selected_positions.csv','eth_v42_policy_comparison.csv','eth_v42_early_shadow_120_105.csv','eth_v42_quote_disappearance.csv','eth_v42_history_readiness.csv')) {
            $source = Join-Path $folder $reportName
            if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $sessionDir $reportName) -Force }
        }
        Log "SESSION_REPORTS=$sessionDir"
    } catch { Log "REPORT_ERROR $($_.Exception.Message)" 'WARN' }
    Log "SUPERVISOR_LOG=$supervisorLog"
    Log "ARCHIVE=$archive"
    if (-not $startupConfirmed) { Log 'STARTUP_NOT_VERIFIED: collector did not demonstrate three increases' 'WARN' }
    if ($SimulateRestart -and -not $recoveryConfirmed) { Log 'RECOVERY_NOT_VERIFIED: test restart did not prove renewed collection' 'WARN' }
}
