param(
    [string]$Username = $env:PUBLIC_ADMIN_USERNAME
)

$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Brak środowiska .venv."
}

Set-Location -LiteralPath $projectDirectory
if ([string]::IsNullOrWhiteSpace($Username)) {
    $Username = Read-Host "Login pierwszego administratora publicznego"
}
& $pythonExecutable -m request_app.cli init-admin $Username
