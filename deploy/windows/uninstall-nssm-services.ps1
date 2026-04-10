[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$NssmPath = "",
    [string]$RuntimeServiceName = "",
    [string]$HealthTaskName = ""
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "load-service-config.ps1")

$configBundle = Get-WindowsServiceConfig -ConfigPath $ConfigPath -WindowsDeployRoot $PSScriptRoot
$serviceConfig = $configBundle.Data

if (-not $RuntimeServiceName) { $RuntimeServiceName = [string]$serviceConfig.RuntimeServiceName }
if (-not $HealthTaskName) { $HealthTaskName = [string]$serviceConfig.HealthTaskName }

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-NssmExecutable {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path $RequestedPath)) {
            throw "nssm.exe not found: $RequestedPath"
        }
        return (Resolve-Path $RequestedPath).Path
    }

    $command = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\nssm.exe"),
        (Join-Path $env:ChocolateyInstall "bin\nssm.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate nssm.exe. Pass -NssmPath when uninstalling services."
}

function Remove-ServiceIfPresent {
    param(
        [string]$ServiceName,
        [string]$NssmExecutable
    )

    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        return
    }

    if ($service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
    }

    & $NssmExecutable remove $ServiceName confirm | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to remove service: $ServiceName"
    }
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell session."
}

$resolvedNssm = Resolve-NssmExecutable -RequestedPath $NssmPath

$task = Get-ScheduledTask -TaskName $HealthTaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $HealthTaskName -Confirm:$false
}

Remove-ServiceIfPresent -ServiceName $RuntimeServiceName -NssmExecutable $resolvedNssm