$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Brak środowiska .venv."
}

Set-Location -LiteralPath $projectDirectory
& $pythonExecutable -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $pythonExecutable tools\security_runtime_smoke.py
