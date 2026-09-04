$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Brak środowiska .venv. Uruchom najpierw konfigurację opisaną w README.md."
}

Set-Location -LiteralPath $projectDirectory
Write-Host "penczREQ Control: http://127.0.0.1:8001" -ForegroundColor Cyan
Write-Host "Panel jest przeznaczony wyłącznie dla sieci lokalnej." -ForegroundColor Yellow
Write-Host "Zatrzymanie serwera: Ctrl+C" -ForegroundColor DarkGray
& $pythonExecutable -m uvicorn request_app.control:app --reload --host 0.0.0.0 --port 8001
