[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SshTarget = "root@188.166.253.203",
    [string]$RemoteProjectRoot = "/root/Crypto_Sniper",
    [string]$RemoteServiceName = "crypto-bot-tmux",
    [string]$BotHealthUrl = "http://127.0.0.1:8080/health",
    [string]$ProjectRoot = "",
    # Defaults tuned for slow post-merge cold starts (health after systemd/tmux + Python init).
    [int]$HealthCheckAttempts = 45,
    [int]$HealthCheckIntervalSeconds = 3,
    [int]$HealthCheckInitialDelaySeconds = 20,
    [switch]$SkipHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$remoteServiceUser = if ($SshTarget -match '^(?<user>[^@]+)@') { $Matches['user'] } else { 'root' }

$runtimeFiles = @(
    'main.py',
    # core/ package
    'core/__init__.py',
    'core/project_paths.py',
    'core/config.py',
    'core/models.py',
    'core/database.py',
    'core/financial_precision.py',
    'core/risk_volatility.py',
    'core/risk_management.py',
    'core/helpers.py',
    'core/log_formatter.py',
    'core/logger_setup.py',
    'core/rate_limiter.py',
    'core/process_guard.py',
    'core/bot_enums.py',
    'core/minimal_roi.py',
    'core/metrics.py',
    # integrations/ package
    'integrations/__init__.py',
    'integrations/api_client.py',
    'integrations/binance_websocket.py',
    'integrations/bitkub_websocket.py',
    'integrations/telegram_bot.py',
    'integrations/alerts.py',
    # bot/ package
    'bot/__init__.py',
    'bot/trading_bot.py',
    'bot/trade_executor.py',
    'bot/signal_generator.py',
    'bot/signal_pipeline.py',
    'bot/strategy_base.py',
    'bot/strategy_runtime_config.py',
    'bot/data_collector.py',
    'bot/portfolio_manager.py',
    'bot/portfolio_rebalancer.py',
    'bot/balance_monitor.py',
    'bot/state_management.py',
    'bot/state_facade.py',
    'bot/monitoring.py',
    'bot/health_server.py',
    'bot/protection_hooks.py',
    'bot/watchdog.py',
    # util/ package
    'util/__init__.py',
    'util/indicators.py',
    'util/multi_timeframe.py',
    'util/hyperopt.py',
    'util/backtesting_validation.py',
    'util/dynamic_coin_config.py',
    # cli/ package (Phase 3)
    'cli/__init__.py',
    'cli/ui.py',
    'cli/command_dispatch.py',
    'cli/layout.py',
    'cli/snapshot_build.py',
    'cli/snapshot_dto.py',
    # root shim stubs (backward-compat — all point to real modules above)
    'project_paths.py',
    'config.py',
    'models.py',
    'database.py',
    'financial_precision.py',
    'risk_volatility.py',
    'risk_management.py',
    'helpers.py',
    'log_formatter.py',
    'logger_setup.py',
    'rate_limiter.py',
    'process_guard.py',
    'bot_enums.py',
    'minimal_roi.py',
    'metrics.py',
    'api_client.py',
    'binance_websocket.py',
    'bitkub_websocket.py',
    'telegram_bot.py',
    'alerts.py',
    'trading_bot.py',
    'trade_executor.py',
    'signal_generator.py',
    'signal_pipeline.py',
    'strategy_base.py',
    'strategy_runtime_config.py',
    'data_collector.py',
    'portfolio_manager.py',
    'portfolio_rebalancer.py',
    'balance_monitor.py',
    'state_management.py',
    'state_facade.py',
    'monitoring.py',
    'health_server.py',
    'protection_hooks.py',
    'watchdog.py',
    'indicators.py',
    'multi_timeframe.py',
    'hyperopt.py',
    'backtesting_validation.py',
    'dynamic_coin_config.py',
    'cli_ui.py',
    'cli_command_dispatch.py',
    'cli_snapshot_build.py',
    'cli_snapshot_dto.py',
    'bot_config.yaml',
    'coin_whitelist.json',
    'strategies/__init__.py',
    'strategies/sniper.py',
    'trading/execution_runtime.py',
    'trading/cost_basis.py',
    'trading/managed_lifecycle.py',
    'trading/portfolio_runtime.py',
    'trading/position_monitor.py',
    'trading/signal_runtime.py',
    'trading/startup_runtime.py',
    'trading/status_runtime.py',
    'trading/bootstrap_config.py',
    'trading/dynamic_config.py',
    'trading/cli_pair_normalize.py',
    'trading/cli_snapshot_builder.py',
    'trading/manual_trading_service.py',
    'trading/runtime_pairlist_service.py',
    'trading/runtime_process.py',
    'trading/bot_runtime/__init__.py',
    'trading/bot_runtime/balance_event_runtime.py',
    'trading/bot_runtime/candle_readiness_filter_runtime.py',
    'trading/bot_runtime/main_loop_runtime.py',
    'trading/bot_runtime/orchestrator_exit_gates_runtime.py',
    'trading/bot_runtime/order_logging_runtime.py',
    'trading/bot_runtime/orchestrator_runtime_deps.py',
    'trading/bot_runtime/pause_state_runtime.py',
    'trading/bot_runtime/pre_trade_gate_runtime.py',
    'trading/bot_runtime/runtime_pairs_runtime.py',
    'trading/bot_runtime/run_iteration_runtime.py',
    'trading/bot_runtime/websocket_runtime.py',
    'deploy/systemd/crypto-bot-tmux.sh',
    'deploy/systemd/crypto-bot-tmux.service',
    'deploy/systemd/crypto-bot-runtime.service',
    'deploy/systemd/crypto-attach',
    'deploy/systemd/vps_switch_to_tmux_only.sh'
)

function Write-Step {
    param([string]$Message)
    Write-Host "[INFO] $Message"
}

function Assert-CommandExists {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$IgnoreExitCode
    )

    & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if (-not $IgnoreExitCode -and $exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')"
    }
    return $exitCode
}

function Invoke-SshScript {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$ScriptBody,
        [switch]$CaptureOutput,
        [switch]$IgnoreExitCode
    )

    $tempFile = [System.IO.Path]::GetTempFileName()
    $remoteTempPath = "/tmp/copilot-deploy-$([System.Guid]::NewGuid().ToString('N')).sh"
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $normalizedScriptBody = $ScriptBody -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($tempFile, $normalizedScriptBody, $utf8NoBom)
        $null = Invoke-External -FilePath 'scp' -Arguments @($tempFile, "${Target}:$remoteTempPath")
        if ($CaptureOutput) {
            $output = & ssh $Target "bash $remoteTempPath"
            $exitCode = $LASTEXITCODE
            $null = & ssh $Target "rm -f $remoteTempPath"
            if (-not $IgnoreExitCode -and $exitCode -ne 0) {
                throw "Remote command failed with exit code $exitCode"
            }
            return $output
        }

        & ssh $Target "bash $remoteTempPath"
        $exitCode = $LASTEXITCODE
        $null = & ssh $Target "rm -f $remoteTempPath"
        if (-not $IgnoreExitCode -and $exitCode -ne 0) {
            throw "Remote command failed with exit code $exitCode"
        }
        return $null
    }
    finally {
        if ($remoteTempPath) {
            $null = & ssh $Target "rm -f $remoteTempPath"
        }
        Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
    }
}

function Convert-ToRemotePath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $normalized = ($RelativePath -replace '\\', '/')
    return "$BasePath/$normalized"
}

function Get-RemoteFileLoopBody {
    param([Parameter(Mandatory = $true)][string[]]$RelativePaths)

    return (($RelativePaths | ForEach-Object { $_ -replace '\\', '/' }) -join "`n")
}

function New-RemoteMirrorScript {
    param(
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string[]]$RelativePaths
    )

    $script = @'
set -euo pipefail
destination_root='__DESTINATION_ROOT__'
project_root='__PROJECT_ROOT__'

mkdir -p "$destination_root"

while IFS= read -r relative_path; do
    [ -n "$relative_path" ] || continue
    src="$project_root/$relative_path"
    dst="$destination_root/$relative_path"
    mkdir -p "$(dirname "$dst")"
    if [ -e "$src" ]; then
        cp "$src" "$dst"
    fi
done <<'FILES'
__RUNTIME_FILES__
FILES

echo "$destination_root"
'@

    return $script.Replace('__DESTINATION_ROOT__', $DestinationRoot).Replace('__PROJECT_ROOT__', $ProjectRoot).Replace('__RUNTIME_FILES__', (Get-RemoteFileLoopBody -RelativePaths $RelativePaths))
}

function New-RemoteMkdirScript {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string[]]$RelativePaths
    )

    $script = @'
set -euo pipefail
project_root='__PROJECT_ROOT__'

mkdir -p "$project_root"

while IFS= read -r relative_path; do
    [ -n "$relative_path" ] || continue
    mkdir -p "$(dirname "$project_root/$relative_path")"
done <<'FILES'
__RUNTIME_FILES__
FILES
'@

    return $script.Replace('__PROJECT_ROOT__', $ProjectRoot).Replace('__RUNTIME_FILES__', (Get-RemoteFileLoopBody -RelativePaths $RelativePaths))
}

function New-RemoteValidateAndRestartScript {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$ServiceUser,
        [Parameter(Mandatory = $true)][string]$ServiceName,
        [Parameter(Mandatory = $true)][string]$HealthUrl,
        [Parameter(Mandatory = $true)][int]$SkipHealth,
        [Parameter(Mandatory = $true)][int]$HealthAttempts,
        [Parameter(Mandatory = $true)][int]$HealthSleepSeconds,
        [Parameter(Mandatory = $true)][int]$HealthInitialDelaySeconds,
        [Parameter(Mandatory = $true)][string[]]$RelativePaths
    )

    $pythonFiles = @($RelativePaths | Where-Object { $_.EndsWith('.py') })
    $script = @'
set -euo pipefail
project_root='__PROJECT_ROOT__'
service_user='__REMOTE_SERVICE_USER__'
service_name='__REMOTE_SERVICE_NAME__'
health_url='__BOT_HEALTH_URL__'
skip_health='__SKIP_HEALTH__'
health_attempts='__HEALTH_ATTEMPTS__'
health_sleep='__HEALTH_SLEEP_SECONDS__'
health_initial_delay='__HEALTH_INITIAL_DELAY_SECONDS__'

resolve_python_bin() {
    for candidate in \
        "$project_root/.venv/bin/python" \
        "$project_root/.venv-3/bin/python" \
        "$project_root/venv/bin/python"
    do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    echo "Could not locate a project Python executable under $project_root" >&2
    return 1
}

chmod +x "$project_root/deploy/systemd/crypto-bot-tmux.sh" \
    "$project_root/deploy/systemd/crypto-attach" \
    "$project_root/deploy/systemd/vps_switch_to_tmux_only.sh"
sed \
    -e "s|^User=.*|User=$service_user|" \
    -e "s|^Group=.*|Group=$service_user|" \
    -e "s|/opt/crypto-bot-v1|$project_root|g" \
    "$project_root/deploy/systemd/crypto-bot-tmux.service" \
    > "/etc/systemd/system/crypto-bot-tmux.service"
sed \
    -e "s|^User=.*|User=$service_user|" \
    -e "s|^Group=.*|Group=$service_user|" \
    -e "s|/opt/crypto-bot-v1|$project_root|g" \
    "$project_root/deploy/systemd/crypto-bot-runtime.service" \
    > "/etc/systemd/system/crypto-bot-runtime.service"
systemctl daemon-reload
cd "$project_root"
python_bin="$(resolve_python_bin)"
"$python_bin" -m py_compile __PYTHON_FILES__
systemctl restart "$service_name"
systemctl status "$service_name" --no-pager -l
tmux list-sessions

if [ "$skip_health" = "0" ]; then
    if [ "$health_initial_delay" -gt 0 ]; then
        sleep "$health_initial_delay"
    fi
    attempt=1
    while [ "$attempt" -le "$health_attempts" ]; do
        if curl -fsS "$health_url"; then
            exit 0
        fi
        attempt=$((attempt + 1))
        sleep "$health_sleep"
    done
    echo "Health check failed: $health_url" >&2
    exit 1
fi
'@

    return $script.Replace('__PROJECT_ROOT__', $ProjectRoot).Replace('__REMOTE_SERVICE_USER__', $ServiceUser).Replace('__REMOTE_SERVICE_NAME__', $ServiceName).Replace('__BOT_HEALTH_URL__', $HealthUrl).Replace('__SKIP_HEALTH__', [string]$SkipHealth).Replace('__HEALTH_ATTEMPTS__', [string]$HealthAttempts).Replace('__HEALTH_SLEEP_SECONDS__', [string]$HealthSleepSeconds).Replace('__HEALTH_INITIAL_DELAY_SECONDS__', [string]$HealthInitialDelaySeconds).Replace('__PYTHON_FILES__', ($pythonFiles -join ' '))
}

Assert-CommandExists -Name 'ssh'
Assert-CommandExists -Name 'scp'

foreach ($relativePath in $runtimeFiles) {
    $localPath = Join-Path $resolvedProjectRoot $relativePath
    if (-not (Test-Path -LiteralPath $localPath)) {
        throw "Required deploy file is missing: $localPath"
    }
}

Write-Step "Deploy target: $SshTarget"
Write-Step "Remote root: $RemoteProjectRoot"

if (-not $PSCmdlet.ShouldProcess($SshTarget, "Deploy runtime files and restart $RemoteServiceName")) {
    return
}

$preDeploySnapshot = "$RemoteProjectRoot/.deploy-backups/pre-$timestamp"
$postDeploySnapshot = "$RemoteProjectRoot/.deploy-snapshots/$timestamp"

Write-Step "Creating pre-deploy backup on VPS"
$backupScript = New-RemoteMirrorScript -DestinationRoot $preDeploySnapshot -ProjectRoot $RemoteProjectRoot -RelativePaths $runtimeFiles
$backupResult = Invoke-SshScript -Target $SshTarget -ScriptBody $backupScript -CaptureOutput
if ($backupResult) {
    Write-Step ("Pre-deploy backup: {0}" -f ($backupResult.Trim()))
}

Write-Step "Ensuring remote directories exist"
$mkdirScript = New-RemoteMkdirScript -ProjectRoot $RemoteProjectRoot -RelativePaths $runtimeFiles
Invoke-SshScript -Target $SshTarget -ScriptBody $mkdirScript

Write-Step "Uploading runtime files"
foreach ($relativePath in $runtimeFiles) {
    $localPath = Join-Path $resolvedProjectRoot $relativePath
    $remotePath = Convert-ToRemotePath -BasePath $RemoteProjectRoot -RelativePath $relativePath
    $null = Invoke-External -FilePath 'scp' -Arguments @($localPath, "${SshTarget}:$remotePath")
}

Write-Step "Validating Python files and restarting service"
$validateAndRestartScript = New-RemoteValidateAndRestartScript -ProjectRoot $RemoteProjectRoot -ServiceUser $remoteServiceUser -ServiceName $RemoteServiceName -HealthUrl $BotHealthUrl -SkipHealth ([int]$SkipHealthCheck.IsPresent) -HealthAttempts $HealthCheckAttempts -HealthSleepSeconds $HealthCheckIntervalSeconds -HealthInitialDelaySeconds $HealthCheckInitialDelaySeconds -RelativePaths $runtimeFiles
Invoke-SshScript -Target $SshTarget -ScriptBody $validateAndRestartScript

Write-Step "Creating post-deploy snapshot on VPS"
$snapshotScript = New-RemoteMirrorScript -DestinationRoot $postDeploySnapshot -ProjectRoot $RemoteProjectRoot -RelativePaths $runtimeFiles
$snapshotResult = Invoke-SshScript -Target $SshTarget -ScriptBody $snapshotScript -CaptureOutput
if ($snapshotResult) {
    Write-Step ("Post-deploy snapshot: {0}" -f ($snapshotResult.Trim()))
}

Write-Step "VPS runtime deploy completed successfully"
