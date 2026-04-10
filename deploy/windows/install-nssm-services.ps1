[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$ConfigPath = "",
    [string]$NssmPath = "",
    [string]$PythonPath = "",
    [string]$RuntimeServiceName = "",
    [string]$HealthTaskName = "",
    [string]$BotHealthUrl = "",
    [Nullable[int]]$FailureThreshold = $null,
    [Nullable[int]]$RestartCooldownSeconds = $null,
    [Nullable[int]]$HealthCheckEveryMinutes = $null,
    [switch]$AllowAuthDegraded,
    [switch]$SkipServiceStart,
    [switch]$SkipTaskRegistration
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "load-service-config.ps1")

$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$configBundle = Get-WindowsServiceConfig -ConfigPath $ConfigPath -WindowsDeployRoot $PSScriptRoot
$serviceConfig = $configBundle.Data

if (-not $RuntimeServiceName) { $RuntimeServiceName = [string]$serviceConfig.RuntimeServiceName }
if (-not $HealthTaskName) { $HealthTaskName = [string]$serviceConfig.HealthTaskName }
if (-not $BotHealthUrl) { $BotHealthUrl = [string]$serviceConfig.BotHealthUrl }
if ($null -eq $FailureThreshold) { $FailureThreshold = [int]$serviceConfig.FailureThreshold }
if ($null -eq $RestartCooldownSeconds) { $RestartCooldownSeconds = [int]$serviceConfig.RestartCooldownSeconds }
if ($null -eq $HealthCheckEveryMinutes) { $HealthCheckEveryMinutes = [int]$serviceConfig.HealthCheckEveryMinutes }
if (-not $AllowAuthDegraded.IsPresent -and $serviceConfig.ContainsKey('AllowAuthDegraded')) {
    $AllowAuthDegraded = [bool]$serviceConfig.AllowAuthDegraded
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-PythonExecutable {
    param(
        [string]$Root,
        [string]$RequestedPath
    )

    if ($RequestedPath) {
        if (-not (Test-Path $RequestedPath)) {
            throw "Python executable not found: $RequestedPath"
        }
        return (Resolve-Path $RequestedPath).Path
    }

    $candidates = @(
        (Join-Path $Root ".venv-3\Scripts\python.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate a project Python executable under $Root"
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
        "C:\nssm\win64\nssm.exe",
        "C:\nssm\win32\nssm.exe",
        (Join-Path $env:ProgramFiles "nssm\win64\nssm.exe"),
        (Join-Path $env:ProgramFiles "nssm\win32\nssm.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\nssm.exe"),
        (Join-Path $env:ChocolateyInstall "bin\nssm.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate nssm.exe. Install NSSM first or pass -NssmPath."
}

function Invoke-Nssm {
    param([string[]]$Arguments)

    & $resolvedNssm @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "nssm command failed: $($Arguments -join ' ')"
    }
}

function Set-NssmRegistryString {
    param(
        [string]$ServiceName,
        [string]$Name,
        [string]$Value
    )

    $parametersKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    if (-not (Test-Path $parametersKey)) {
        New-Item -Path $parametersKey -Force | Out-Null
    }

    New-ItemProperty -Path $parametersKey -Name $Name -Value $Value -PropertyType String -Force | Out-Null
}

function Get-ServiceExists {
    param([string]$Name)

    return $null -ne (Get-Service -Name $Name -ErrorAction SilentlyContinue)
}

function Configure-Service {
    param(
        [string]$ServiceName,
        [string]$ScriptPath,
        [string]$ScriptArguments,
        [string]$StdoutPath,
        [string]$StderrPath,
        [string]$Description
    )

    $appParameters = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $ScriptArguments"

    if (Get-ServiceExists -Name $ServiceName) {
        Invoke-Nssm -Arguments @("set", $ServiceName, "Application", $powerShellExe)
    }
    else {
        Invoke-Nssm -Arguments @("install", $ServiceName, $powerShellExe, $appParameters)
    }

    Set-NssmRegistryString -ServiceName $ServiceName -Name "AppParameters" -Value $appParameters

    Invoke-Nssm -Arguments @("set", $ServiceName, "AppDirectory", $resolvedRoot)
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppStdout", $StdoutPath)
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppStderr", $StderrPath)
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRotateFiles", "1")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRotateOnline", "1")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppRotateBytes", "10485760")
    Invoke-Nssm -Arguments @("set", $ServiceName, "Start", "SERVICE_AUTO_START")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppExit", "Default", "Restart")
    Invoke-Nssm -Arguments @("set", $ServiceName, "AppThrottle", "1500")

    sc.exe description $ServiceName $Description | Out-Null
}

function Restart-Or-StartService {
    param([string]$Name)

    $service = Get-Service -Name $Name -ErrorAction Stop
    if ($service.Status -eq "Running") {
        Restart-Service -Name $Name -Force -ErrorAction Stop
    }
    elseif ($service.Status -eq "Paused") {
        Stop-Service -Name $Name -Force -ErrorAction Stop
        $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
        Start-Service -Name $Name -ErrorAction Stop
    }
    else {
        Start-Service -Name $Name -ErrorAction Stop
    }
}

function Register-HealthTask {
    $healthScript = Join-Path $resolvedRoot "deploy\windows\invoke-health-check.ps1"
    $stateFile = Join-Path $resolvedRoot "logs\windows-service-health-state.json"

    $taskArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $healthScript),
        "-ConfigPath", ('"{0}"' -f $configBundle.Path),
        "-RuntimeServiceName", ('"{0}"' -f $RuntimeServiceName),
        "-BotHealthUrl", ('"{0}"' -f $BotHealthUrl),
        "-FailureThreshold", $FailureThreshold,
        "-RestartCooldownSeconds", $RestartCooldownSeconds,
        "-StateFile", ('"{0}"' -f $stateFile)
    )

    if ($AllowAuthDegraded) {
        $taskArguments += "-AllowAuthDegraded"
    }

    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $HealthCheckEveryMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
    $action = New-ScheduledTaskAction -Execute $powerShellExe -Argument ($taskArguments -join ' ')
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable

    Register-ScheduledTask -TaskName $HealthTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell session."
}

$resolvedPython = Resolve-PythonExecutable -Root $resolvedRoot -RequestedPath $PythonPath
$resolvedNssm = Resolve-NssmExecutable -RequestedPath $NssmPath

$runtimeScript = Join-Path $resolvedRoot "deploy\windows\run-runtime.ps1"
$serviceLogDir = Join-Path $resolvedRoot "logs\services"

New-Item -ItemType Directory -Path $serviceLogDir -Force | Out-Null

$runtimeArgs = "-ProjectRoot `"$resolvedRoot`" -PythonPath `"$resolvedPython`""

Configure-Service -ServiceName $RuntimeServiceName -ScriptPath $runtimeScript -ScriptArguments $runtimeArgs -StdoutPath (Join-Path $serviceLogDir "runtime-service.log") -StderrPath (Join-Path $serviceLogDir "runtime-service.err.log") -Description "Crypto Bot trading runtime"

Write-Info "Configured NSSM service: $RuntimeServiceName"
Write-Info "Using service config: $($configBundle.Path)"

if (-not $SkipTaskRegistration) {
    Register-HealthTask
    Write-Info "Registered scheduled health monitor task: $HealthTaskName"
}

if (-not $SkipServiceStart) {
    Restart-Or-StartService -Name $RuntimeServiceName
    Write-Info "Started service: $RuntimeServiceName"
}

Write-Info "Runtime health URL: $BotHealthUrl"
Write-Info "Service logs: $serviceLogDir"