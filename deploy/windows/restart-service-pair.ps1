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

$newScript = Join-Path $PSScriptRoot "restart-runtime-service.ps1"
if (-not (Test-Path $newScript)) {
    throw "Expected runtime restart script not found: $newScript"
}

Write-Warning "restart-service-pair.ps1 is deprecated. Use restart-runtime-service.ps1 instead."

$arguments = @{}
if ($ConfigPath) { $arguments.ConfigPath = $ConfigPath }
if ($RuntimeServiceName) { $arguments.RuntimeServiceName = $RuntimeServiceName }
if ($BotHealthUrl) { $arguments.BotHealthUrl = $BotHealthUrl }
if ($null -ne $RuntimeStartTimeoutSeconds) { $arguments.RuntimeStartTimeoutSeconds = $RuntimeStartTimeoutSeconds }
if ($StateFile) { $arguments.StateFile = $StateFile }
if ($AllowAuthDegraded.IsPresent) { $arguments.AllowAuthDegraded = $true }

& $newScript @arguments
exit $LASTEXITCODE