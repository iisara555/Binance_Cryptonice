[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SshTarget = "root@188.166.253.203",
    [string]$RemoteProjectRoot = "/root/Crypto_Sniper",
    [string]$RemoteServiceName = "crypto-bot-tmux",
    [string]$BotHealthUrl = "http://127.0.0.1:8080/health",
    [string]$ProjectRoot = "",
    [int]$HealthCheckAttempts = 20,
    [int]$HealthCheckIntervalSeconds = 2,
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
    'alerts.py',
    'main.py',
    'api_client.py',
    'helpers.py',
    'portfolio_manager.py',
    'data_collector.py',
    'bitkub_websocket.py',
    'trading_bot.py',
    'trade_executor.py',
    'balance_monitor.py',
    'database.py',
    'multi_timeframe.py',
    'risk_management.py',
    'signal_generator.py',
    'strategy_base.py',
    'telegram_bot.py',
    'cli_ui.py',
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
    'deploy/systemd/crypto-bot-tmux.sh',
    'deploy/systemd/crypto-bot-tmux.service',
    'deploy/systemd/crypto-bot-runtime.service'
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

chmod +x "$project_root/deploy/systemd/crypto-bot-tmux.sh"
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

    return $script.Replace('__PROJECT_ROOT__', $ProjectRoot).Replace('__REMOTE_SERVICE_USER__', $ServiceUser).Replace('__REMOTE_SERVICE_NAME__', $ServiceName).Replace('__BOT_HEALTH_URL__', $HealthUrl).Replace('__SKIP_HEALTH__', [string]$SkipHealth).Replace('__HEALTH_ATTEMPTS__', [string]$HealthAttempts).Replace('__HEALTH_SLEEP_SECONDS__', [string]$HealthSleepSeconds).Replace('__PYTHON_FILES__', ($pythonFiles -join ' '))
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
$validateAndRestartScript = New-RemoteValidateAndRestartScript -ProjectRoot $RemoteProjectRoot -ServiceUser $remoteServiceUser -ServiceName $RemoteServiceName -HealthUrl $BotHealthUrl -SkipHealth ([int]$SkipHealthCheck.IsPresent) -HealthAttempts $HealthCheckAttempts -HealthSleepSeconds $HealthCheckIntervalSeconds -RelativePaths $runtimeFiles
Invoke-SshScript -Target $SshTarget -ScriptBody $validateAndRestartScript

Write-Step "Creating post-deploy snapshot on VPS"
$snapshotScript = New-RemoteMirrorScript -DestinationRoot $postDeploySnapshot -ProjectRoot $RemoteProjectRoot -RelativePaths $runtimeFiles
$snapshotResult = Invoke-SshScript -Target $SshTarget -ScriptBody $snapshotScript -CaptureOutput
if ($snapshotResult) {
    Write-Step ("Post-deploy snapshot: {0}" -f ($snapshotResult.Trim()))
}

Write-Step "VPS runtime deploy completed successfully"