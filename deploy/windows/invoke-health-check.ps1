[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$RuntimeServiceName = "",
    [string]$BotHealthUrl = "",
    [Nullable[int]]$FailureThreshold = $null,
    [Nullable[int]]$RestartCooldownSeconds = $null,
    [Nullable[int]]$RuntimeStartTimeoutSeconds = $null,
    [string]$StateFile = "",
    [switch]$AllowAuthDegraded,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "load-service-config.ps1")

$configBundle = Get-WindowsServiceConfig -ConfigPath $ConfigPath -WindowsDeployRoot $PSScriptRoot
$serviceConfig = $configBundle.Data

if (-not $RuntimeServiceName) { $RuntimeServiceName = [string]$serviceConfig.RuntimeServiceName }
if (-not $BotHealthUrl) { $BotHealthUrl = [string]$serviceConfig.BotHealthUrl }
if ($null -eq $FailureThreshold) { $FailureThreshold = [int]$serviceConfig.FailureThreshold }
if ($null -eq $RestartCooldownSeconds) { $RestartCooldownSeconds = [int]$serviceConfig.RestartCooldownSeconds }
if ($null -eq $RuntimeStartTimeoutSeconds) { $RuntimeStartTimeoutSeconds = [int]$serviceConfig.RuntimeStartTimeoutSeconds }
if (-not $AllowAuthDegraded.IsPresent -and $serviceConfig.ContainsKey('AllowAuthDegraded')) {
    $AllowAuthDegraded = [bool]$serviceConfig.AllowAuthDegraded
}

if (-not $StateFile) {
    $StateFile = Join-Path (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path "logs") "windows-service-health-state.json"
}

$stateDir = Split-Path -Parent $StateFile
$logFile = Join-Path $stateDir "windows-service-health-monitor.log"

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

function Write-HealthLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] $Message"
}

function New-StateObject {
    return @{
        consecutive_failures = 0
        last_restart_utc = $null
        last_check_utc = $null
        last_result = @{
            runtime = @{ healthy = $false; status = "unknown"; detail = "" }
        }
    }
}

function Get-StateObject {
    $state = New-StateObject
    if (-not (Test-Path $StateFile)) { return $state }

    try {
        $raw = Get-Content -Path $StateFile -Raw | ConvertFrom-Json
        if ($null -ne $raw.consecutive_failures) { $state.consecutive_failures = [int]$raw.consecutive_failures }
        if ($raw.last_restart_utc) { $state.last_restart_utc = [string]$raw.last_restart_utc }
        if ($raw.last_check_utc) { $state.last_check_utc = [string]$raw.last_check_utc }
        if ($raw.last_result.runtime) {
            $state.last_result.runtime.healthy = [bool]$raw.last_result.runtime.healthy
            $state.last_result.runtime.status = [string]$raw.last_result.runtime.status
            $state.last_result.runtime.detail = [string]$raw.last_result.runtime.detail
        }
    }
    catch {
        Write-HealthLog "State file parse failed, resetting state: $($_.Exception.Message)"
    }
    return $state
}

function Save-StateObject {
    param([hashtable]$State)
    $State | ConvertTo-Json -Depth 5 | Set-Content -Path $StateFile -Encoding UTF8
}

function Test-EndpointHealth {
    param([string]$Url, [bool]$AllowDegraded)

    try {
        $payload = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 8
        $healthy = [bool]$payload.healthy
        $status = [string]$payload.status
        if (-not $status) { $status = if ($healthy) { "ok" } else { "unknown" } }
        if ($healthy -and (-not $AllowDegraded) -and $status -eq "degraded") { $healthy = $false }
        return @{ healthy = $healthy; status = $status; detail = "" }
    }
    catch {
        return @{ healthy = $false; status = "error"; detail = $_.Exception.Message }
    }
}

function Get-HealthSummary {
    param([hashtable]$Result)
    $summary = [string]$Result.status
    if ($Result.detail) { $summary = "$summary ($($Result.detail))" }
    return $summary
}

function Wait-EndpointHealthy {
    param([string]$Url, [int]$TimeoutSeconds, [bool]$AllowDegraded)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $probe = Test-EndpointHealth -Url $Url -AllowDegraded:$AllowDegraded
        if ($probe.healthy) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-ServiceOrNull {
    param([string]$Name)
    try { return Get-Service -Name $Name -ErrorAction Stop }
    catch { return $null }
}

function Stop-ServiceIfPresent {
    param([string]$Name)
    $service = Get-ServiceOrNull -Name $Name
    if ($null -eq $service -or $service.Status -eq "Stopped") { return }
    Stop-Service -Name $Name -Force -ErrorAction Stop
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}

function Start-ServiceIfPresent {
    param([string]$Name)
    $service = Get-ServiceOrNull -Name $Name
    if ($null -eq $service) { throw "Windows service not found: $Name" }
    if ($service.Status -ne "Running") {
        Start-Service -Name $Name -ErrorAction Stop
        $service.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
    }
}

function Invoke-RuntimeRestart {
    Write-HealthLog "Failure threshold reached. Restarting runtime service."
    Stop-ServiceIfPresent -Name $RuntimeServiceName
    Start-Sleep -Seconds 2
    Start-ServiceIfPresent -Name $RuntimeServiceName
    $runtimeReady = Wait-EndpointHealthy -Url $BotHealthUrl -TimeoutSeconds $RuntimeStartTimeoutSeconds -AllowDegraded:$AllowAuthDegraded.IsPresent
    if (-not $runtimeReady) {
        Write-HealthLog "Runtime health did not return to OK within $RuntimeStartTimeoutSeconds seconds."
    }
    return $runtimeReady
}

# ── Main Logic ────────────────────────────────────────────────────────────────

$state = Get-StateObject

if ($ForceRestart.IsPresent) {
    Write-HealthLog "Forced restart requested."
    $restartOk = Invoke-RuntimeRestart
    $runtimeResult = Test-EndpointHealth -Url $BotHealthUrl -AllowDegraded:$AllowAuthDegraded.IsPresent

    $state.consecutive_failures = 0
    $state.last_check_utc = (Get-Date).ToUniversalTime().ToString("o")
    $state.last_restart_utc = (Get-Date).ToUniversalTime().ToString("o")
    $state.last_result.runtime = $runtimeResult
    Save-StateObject -State $state

    if ($runtimeResult.healthy) {
        Write-HealthLog "Forced restart completed successfully."
        exit 0
    }

    Write-HealthLog "Forced restart finished but health not restored. runtime=$(Get-HealthSummary -Result $runtimeResult)"
    exit 1
}

$runtimeResult = Test-EndpointHealth -Url $BotHealthUrl -AllowDegraded:$AllowAuthDegraded.IsPresent

$state.last_check_utc = (Get-Date).ToUniversalTime().ToString("o")
$state.last_result.runtime = $runtimeResult

if ($runtimeResult.healthy) {
    $state.consecutive_failures = 0
    Save-StateObject -State $state
    exit 0
}

$state.consecutive_failures = [int]$state.consecutive_failures + 1
Write-HealthLog "Health check failed (#$($state.consecutive_failures)/$FailureThreshold). runtime=$(Get-HealthSummary -Result $runtimeResult)"

$cooldownActive = $false
if ($state.last_restart_utc) {
    $lastRestart = [DateTime]::Parse($state.last_restart_utc).ToUniversalTime()
    $elapsedSeconds = ((Get-Date).ToUniversalTime() - $lastRestart).TotalSeconds
    $cooldownActive = $elapsedSeconds -lt $RestartCooldownSeconds
}

if ($state.consecutive_failures -lt $FailureThreshold) {
    Save-StateObject -State $state
    exit 0
}

if ($cooldownActive) {
    Write-HealthLog "Restart suppressed because cooldown ($RestartCooldownSeconds s) is still active."
    Save-StateObject -State $state
    exit 0
}

$restartOk = Invoke-RuntimeRestart
$runtimeResult = Test-EndpointHealth -Url $BotHealthUrl -AllowDegraded:$AllowAuthDegraded.IsPresent
$state.consecutive_failures = 0
$state.last_restart_utc = (Get-Date).ToUniversalTime().ToString("o")
$state.last_check_utc = (Get-Date).ToUniversalTime().ToString("o")
$state.last_result.runtime = $runtimeResult
Save-StateObject -State $state

if ($runtimeResult.healthy) { exit 0 }

if ($restartOk) {
    Write-HealthLog "Post-restart health still not restored. runtime=$(Get-HealthSummary -Result $runtimeResult)"
}

exit 1
