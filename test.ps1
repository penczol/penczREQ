$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

Set-Location -LiteralPath $projectDirectory
& $pythonExecutable -m pytest -q
