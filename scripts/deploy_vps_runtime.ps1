[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SshTarget = "root@188.166.253.203",
    [string]$RemoteProjectRoot = "/root/Crypto_Sniper",
    [string]$RemoteServiceName = "crypto-bot-tmux",
    [string]$BotHealthUrl = "http://127.0.0.1:8080/health",
    [string]$ProjectRoot = "",
    [switch]$SkipHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

$runtimeFiles = @(
    'main.py',
    'api_client.py',
    'data_collector.py',
    'trading_bot.py',
    'trade_executor.py',
    'balance_monitor.py',
    'database.py',
    'risk_management.py',
    'signal_generator.py',
    'cli_ui.py',
    'bot_config.yaml',
    'coin_whitelist.json',
    'deploy/systemd/crypto-bot-tmux.sh',
    'deploy/systemd/crypto-bot-tmux.service'
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
$backupScript = @'
set -euo pipefail
backup_dir='__PRE_DEPLOY_SNAPSHOT__'
project_root='__REMOTE_PROJECT_ROOT__'
mkdir -p "$backup_dir/deploy/systemd"
cp "$project_root/main.py" "$project_root/api_client.py" "$project_root/data_collector.py" "$project_root/trading_bot.py" "$project_root/trade_executor.py" "$project_root/balance_monitor.py" "$project_root/database.py" "$project_root/risk_management.py" "$project_root/signal_generator.py" "$project_root/cli_ui.py" "$project_root/bot_config.yaml" "$project_root/coin_whitelist.json" "$backup_dir/"
cp "$project_root/deploy/systemd/crypto-bot-tmux.sh" "$project_root/deploy/systemd/crypto-bot-tmux.service" "$backup_dir/deploy/systemd/" 2>/dev/null || true
echo "$backup_dir"
'@
$backupScript = $backupScript.Replace('__PRE_DEPLOY_SNAPSHOT__', $preDeploySnapshot).Replace('__REMOTE_PROJECT_ROOT__', $RemoteProjectRoot)
$backupResult = Invoke-SshScript -Target $SshTarget -ScriptBody $backupScript -CaptureOutput
if ($backupResult) {
    Write-Step ("Pre-deploy backup: {0}" -f ($backupResult.Trim()))
}

Write-Step "Uploading runtime files"
foreach ($relativePath in $runtimeFiles) {
    $localPath = Join-Path $resolvedProjectRoot $relativePath
    $remotePath = Convert-ToRemotePath -BasePath $RemoteProjectRoot -RelativePath $relativePath
    $null = Invoke-External -FilePath 'scp' -Arguments @($localPath, "${SshTarget}:$remotePath")
}

Write-Step "Validating Python files and restarting service"
$validateAndRestartScript = @'
set -euo pipefail
project_root='__REMOTE_PROJECT_ROOT__'
service_name='__REMOTE_SERVICE_NAME__'
health_url='__BOT_HEALTH_URL__'
skip_health='__SKIP_HEALTH__'

chmod +x "$project_root/deploy/systemd/crypto-bot-tmux.sh"
cd "$project_root"
./.venv-3/bin/python -m py_compile main.py api_client.py data_collector.py trading_bot.py trade_executor.py balance_monitor.py database.py risk_management.py signal_generator.py cli_ui.py
systemctl restart "$service_name"
systemctl status "$service_name" --no-pager -l
tmux list-sessions

if [ "$skip_health" = "0" ]; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -fsS "$health_url"; then
            exit 0
        fi
        sleep 2
    done
    echo "Health check failed: $health_url" >&2
    exit 1
fi
'@
$validateAndRestartScript = $validateAndRestartScript.Replace('__REMOTE_PROJECT_ROOT__', $RemoteProjectRoot).Replace('__REMOTE_SERVICE_NAME__', $RemoteServiceName).Replace('__BOT_HEALTH_URL__', $BotHealthUrl).Replace('__SKIP_HEALTH__', ([int]$SkipHealthCheck.IsPresent))
Invoke-SshScript -Target $SshTarget -ScriptBody $validateAndRestartScript

Write-Step "Creating post-deploy snapshot on VPS"
$snapshotScript = @'
set -euo pipefail
snapshot_dir='__POST_DEPLOY_SNAPSHOT__'
project_root='__REMOTE_PROJECT_ROOT__'
mkdir -p "$snapshot_dir/deploy/systemd"
cp "$project_root/main.py" "$project_root/api_client.py" "$project_root/data_collector.py" "$project_root/trading_bot.py" "$project_root/trade_executor.py" "$project_root/balance_monitor.py" "$project_root/database.py" "$project_root/risk_management.py" "$project_root/signal_generator.py" "$project_root/cli_ui.py" "$project_root/bot_config.yaml" "$project_root/coin_whitelist.json" "$snapshot_dir/"
cp "$project_root/deploy/systemd/crypto-bot-tmux.sh" "$project_root/deploy/systemd/crypto-bot-tmux.service" "$snapshot_dir/deploy/systemd/"
echo "$snapshot_dir"
'@
$snapshotScript = $snapshotScript.Replace('__POST_DEPLOY_SNAPSHOT__', $postDeploySnapshot).Replace('__REMOTE_PROJECT_ROOT__', $RemoteProjectRoot)
$snapshotResult = Invoke-SshScript -Target $SshTarget -ScriptBody $snapshotScript -CaptureOutput
if ($snapshotResult) {
    Write-Step ("Post-deploy snapshot: {0}" -f ($snapshotResult.Trim()))
}

Write-Step "VPS runtime deploy completed successfully"