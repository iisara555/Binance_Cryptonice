function Get-WindowsServiceConfig {
    param(
        [string]$ConfigPath,
        [string]$WindowsDeployRoot = $PSScriptRoot
    )

    $resolvedConfigPath = $ConfigPath
    if (-not $resolvedConfigPath) {
        $resolvedConfigPath = Join-Path $WindowsDeployRoot "windows-service-config.psd1"
    }

    if (-not (Test-Path $resolvedConfigPath)) {
        throw "Windows service config file not found: $resolvedConfigPath"
    }

    $config = Import-PowerShellDataFile -Path $resolvedConfigPath
    if (-not $config) {
        throw "Windows service config file is empty: $resolvedConfigPath"
    }

    return @{
        Path = (Resolve-Path $resolvedConfigPath).Path
        Data = $config
    }
}