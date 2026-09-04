$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Brak środowiska .venv. Uruchom najpierw konfigurację opisaną w README.md."
}

Set-Location -LiteralPath $projectDirectory
Write-Host "Requesty lokalnie: http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Na telefonie: http://ADRES_IP_TEGO_KOMPUTERA:8000" -ForegroundColor Cyan
Write-Host "Panel Control uruchom osobno przez .\\start-control-dev.ps1" -ForegroundColor Yellow
Write-Host "Zatrzymanie serwera: Ctrl+C" -ForegroundColor DarkGray
& $pythonExecutable -m uvicorn request_app.main:app --reload --host 0.0.0.0 --port 8000
