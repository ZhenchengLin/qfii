# Start the Qfii debate page on Windows (set up by scripts/windows-setup.ps1).
# Opens http://127.0.0.1:3000 ; close this window (or press Ctrl+C) to stop.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONUTF8 = "1"   # prompts and data files are UTF-8

if (Test-Path ".env.local") {
    Get-Content ".env.local" | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$') {
            Set-Item "env:$($Matches[1])" $Matches[2]
        }
    }
}

# Ollama normally runs from the tray: ask its API, so this works in a window opened
# before Ollama was installed, and start it only if nothing answers
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
try {
    Invoke-WebRequest "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 3 | Out-Null
} catch {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    if (-not (Test-Path $ollama)) { throw "Ollama not found: run scripts\windows-setup.ps1 first" }
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

$cfg = "config.local.toml"
if (-not (Test-Path $cfg)) { throw "config.local.toml not found: run scripts\windows-setup.ps1 first" }

Start-Process powershell -WindowStyle Hidden -ArgumentList `
    "-NoProfile -Command Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:3000/'"
$ErrorActionPreference = "Continue"
& .venv\Scripts\python.exe -m qfii.cli --config $cfg ui
