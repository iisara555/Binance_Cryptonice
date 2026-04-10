[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$KeepTimestampedLogCount = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$removedItems = [System.Collections.Generic.List[string]]::new()
$keptItems = [System.Collections.Generic.List[string]]::new()

function Test-ExcludedPath {
    param(
        [string]$FullPath
    )

    return $FullPath -match '\\node_modules\\' -or $FullPath -match '\\.venv(?:-[^\\]+)?\\'
}

function Remove-TrackedItem {
    param(
        [string]$LiteralPath,
        [string]$Reason
    )

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }

    if ($PSCmdlet.ShouldProcess($LiteralPath, "Remove $Reason")) {
        Remove-Item -LiteralPath $LiteralPath -Recurse -Force
        $removedItems.Add($LiteralPath)
    }
}

function Remove-OldLogsByName {
    param(
        [string]$DirectoryPath,
        [string]$NameRegex,
        [int]$KeepCount = 1
    )

    if (-not (Test-Path -LiteralPath $DirectoryPath)) {
        return
    }

    $matches = Get-ChildItem -LiteralPath $DirectoryPath -File | Where-Object {
        $_.Name -match $NameRegex
    } | Sort-Object Name -Descending

    $matches | Select-Object -Skip $KeepCount | ForEach-Object {
        Remove-TrackedItem -LiteralPath $_.FullName -Reason 'older log snapshot'
    }

    $matches | Select-Object -First $KeepCount | ForEach-Object {
        $keptItems.Add($_.FullName)
    }
}

function Remove-OldLogsByWriteTime {
    param(
        [string]$DirectoryPath,
        [string]$NameRegex,
        [int]$KeepCount = 1
    )

    if (-not (Test-Path -LiteralPath $DirectoryPath)) {
        return
    }

    $matches = Get-ChildItem -LiteralPath $DirectoryPath -File | Where-Object {
        $_.Name -match $NameRegex
    } | Sort-Object -Property @(
        @{ Expression = 'LastWriteTime'; Descending = $true },
        @{ Expression = 'Name'; Descending = $true }
    )

    $matches | Select-Object -Skip $KeepCount | ForEach-Object {
        Remove-TrackedItem -LiteralPath $_.FullName -Reason 'older log snapshot'
    }

    $matches | Select-Object -First $KeepCount | ForEach-Object {
        $keptItems.Add($_.FullName)
    }
}

Write-Output "Project root: $resolvedProjectRoot"

$cacheDirectories = Get-ChildItem -LiteralPath $resolvedProjectRoot -Recurse -Force -Directory | Where-Object {
    $_.Name -eq '__pycache__' -and -not (Test-ExcludedPath -FullPath $_.FullName)
}

foreach ($directory in $cacheDirectories) {
    Remove-TrackedItem -LiteralPath $directory.FullName -Reason 'Python cache directory'
}

$pycFiles = Get-ChildItem -LiteralPath $resolvedProjectRoot -Recurse -Force -File | Where-Object {
    $_.Extension -eq '.pyc' -and -not (Test-ExcludedPath -FullPath $_.FullName)
}

foreach ($file in $pycFiles) {
    Remove-TrackedItem -LiteralPath $file.FullName -Reason 'compiled Python bytecode'
}

Remove-TrackedItem -LiteralPath (Join-Path $resolvedProjectRoot '.pytest_cache') -Reason 'pytest cache'

$duplicateDbPath = Join-Path $resolvedProjectRoot 'trading_bot.db'
if (Test-Path -LiteralPath $duplicateDbPath) {
    $duplicateDb = Get-Item -LiteralPath $duplicateDbPath
    if ($duplicateDb.Length -eq 0) {
        Remove-TrackedItem -LiteralPath $duplicateDb.FullName -Reason 'unreferenced empty database stub'
    }
    else {
        $keptItems.Add($duplicateDb.FullName)
    }
}

$logsRoot = Join-Path $resolvedProjectRoot 'logs'
Remove-OldLogsByName -DirectoryPath $logsRoot -NameRegex '^debug\.log\.\d{4}-\d{2}-\d{2}$' -KeepCount $KeepTimestampedLogCount
Remove-OldLogsByWriteTime -DirectoryPath $logsRoot -NameRegex '^windows-elevated-install(?:-\d+)?\.log$' -KeepCount 1

$serviceLogsRoot = Join-Path $logsRoot 'services'
Remove-OldLogsByName -DirectoryPath $serviceLogsRoot -NameRegex '^runtime-service-\d{8}T\d{6}\.\d{3}\.log$' -KeepCount $KeepTimestampedLogCount
Remove-OldLogsByName -DirectoryPath $serviceLogsRoot -NameRegex '^runtime-service\.err-\d{8}T\d{6}\.\d{3}\.log$' -KeepCount $KeepTimestampedLogCount

Write-Output ''
Write-Output ('Removed items: {0}' -f $removedItems.Count)
foreach ($item in $removedItems) {
    Write-Output (" - $item")
}

if ($keptItems.Count -gt 0) {
    Write-Output ''
    Write-Output ('Kept items: {0}' -f $keptItems.Count)
    foreach ($item in ($keptItems | Sort-Object -Unique)) {
        Write-Output (" - $item")
    }
}