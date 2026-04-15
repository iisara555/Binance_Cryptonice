[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-PythonPath {
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
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root ".venv-3\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Could not locate a project Python executable under $Root"
}

$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$resolvedPython = Resolve-PythonPath -Root $resolvedRoot -RequestedPath $PythonPath

$env:PYTHONUNBUFFERED = "1"

Set-Location $resolvedRoot
& $resolvedPython (Join-Path $resolvedRoot "main.py")
exit $LASTEXITCODE