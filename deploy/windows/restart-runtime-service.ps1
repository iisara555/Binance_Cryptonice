[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$RuntimeServiceName = "",
    [string]$BotHealthUrl = "",
    [Nullable[int]]$RuntimeStartTimeoutSeconds = $null,
    [string]$StateFile = "",
    [switch]$AllowAuthDegraded
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell session."
}

$healthScript = Join-Path $PSScriptRoot "invoke-health-check.ps1"
$arguments = @{ ForceRestart = $true }

if ($ConfigPath) { $arguments.ConfigPath = $ConfigPath }
if ($RuntimeServiceName) { $arguments.RuntimeServiceName = $RuntimeServiceName }
if ($BotHealthUrl) { $arguments.BotHealthUrl = $BotHealthUrl }
if ($null -ne $RuntimeStartTimeoutSeconds) { $arguments.RuntimeStartTimeoutSeconds = $RuntimeStartTimeoutSeconds }
if ($StateFile) { $arguments.StateFile = $StateFile }
if ($AllowAuthDegraded.IsPresent) { $arguments.AllowAuthDegraded = $true }

Write-Host "[INFO] Running runtime service restart..."
& $healthScript @arguments
exit $LASTEXITCODE
