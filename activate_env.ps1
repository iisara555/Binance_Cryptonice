[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $root ".venv-3\Scripts\Activate.ps1"),
    (Join-Path $root ".venv\Scripts\Activate.ps1"),
    (Join-Path $root "venv\Scripts\Activate.ps1")
)

$activateScript = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $activateScript) {
    throw "Could not find a local virtual environment activate script under $root"
}

. $activateScript