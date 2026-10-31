# Qfii on a Windows PC: install, pick local models (Ollama) that fit this PC,
# fetch data, and add a "Qfii" shortcut that opens the debate page.
#
# Run in PowerShell (no admin needed):
#   irm https://raw.githubusercontent.com/ZhenchengLin/qfii/main/scripts/windows-setup.ps1 -OutFile $env:TEMP\qfii-setup.ps1
#   powershell -ExecutionPolicy Bypass -File $env:TEMP\qfii-setup.ps1
#
# Options:
#   -Plan cpu|gpu8|gpu12|gpu24|gpu48   force a model plan instead of detecting one
#   -SkipData                          do not download prices / macro / SEC data now
# Re-running is safe: it pulls the latest code and keeps your data and .env.local.

param(
    [ValidateSet("", "cpu", "gpu8", "gpu12", "gpu24", "gpu48")]
    [string]$Plan = "",
    [switch]$SkipData
)
$ErrorActionPreference = "Stop"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Need($cmd, $wingetId, $what) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "$what is not installed."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "Install $what now with winget ($wingetId)? [Y/n]"
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            winget install -e --id $wingetId
            Refresh-Path
        }
    }
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "$what is required. Install it, open a NEW PowerShell window and run this script again."
    }
}

function Find-Python {
    # the py launcher first (python.org installs), then python on PATH
    foreach ($v in "3.12", "3.13", "3.11") {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ok = & python -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($ok -eq "True") { return (& python -c "import sys; print(sys.executable)").Trim() }
    }
    return $null
}

function Write-Utf8NoBom($path, $text) {
    # tomllib rejects a byte-order mark, which Windows PowerShell 5 adds by default
    [IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------- tools
Step "Checking Git, Python 3.11+ and Ollama"
Need git "Git.Git" "Git"
$python = Find-Python
if (-not $python) {
    Need winget "Microsoft.AppInstaller" "winget"
    winget install -e --id Python.Python.3.12
    Refresh-Path
    $python = Find-Python
    if (-not $python) { throw "Python 3.11+ not found. Open a NEW PowerShell window and run this script again." }
}
Write-Host "Python: $python"
Need ollama "Ollama.Ollama" "Ollama"

# ---------------------------------------------------------------- code
$docs = [Environment]::GetFolderPath("MyDocuments")
$repo = Join-Path $docs "qfii"
Step "Getting the code into $repo"
if (Test-Path (Join-Path $repo ".git")) {
    git -C $repo pull --ff-only
} else {
    git clone https://github.com/ZhenchengLin/qfii.git $repo
}
if ($LASTEXITCODE -ne 0) { throw "git failed" }
Set-Location $repo

Step "Installing Qfii into .venv"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $python -m venv .venv }
$vpy = Join-Path $repo ".venv\Scripts\python.exe"
& $vpy -m pip install -q --upgrade pip
& $vpy -m pip install -q -e ".[sec]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
$env:PYTHONUTF8 = "1"   # prompts and data files are UTF-8

# ---------------------------------------------------------------- this PC
Step "Reading this PC's specs"
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name.Trim()
$gpu = ""
$vramGB = 0
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $line = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits |
            Select-Object -First 1
    if ($line) {
        $parts = $line -split ","
        $gpu = $parts[0].Trim()
        $vramGB = [math]::Floor([double]$parts[1].Trim() / 1024)
    }
}
if (-not $gpu) {
    $gpu = (Get-CimInstance Win32_VideoController | Select-Object -First 1).Name
    $gpu = "$gpu (no NVIDIA GPU detected: models run on the CPU)"
}
Write-Host "CPU:  $cpu"
Write-Host "RAM:  $ramGB GB"
Write-Host "GPU:  $gpu$(if ($vramGB) { ", $vramGB GB VRAM" })"

if (-not $Plan) {
    if ($vramGB -ge 40) { $Plan = "gpu48" }
    elseif ($vramGB -ge 20) { $Plan = "gpu24" }
    elseif ($vramGB -ge 12) { $Plan = "gpu12" }
    elseif ($vramGB -ge 8) { $Plan = "gpu8" }
    else { $Plan = "cpu" }
}

# Model plans, smallest to largest. Tiers: mid = Research; strong = Assessment,
# Risk, Aggressive, Conservative; strongest = Neutral.
$qwen9 = @"
provider = "ollama"
model = "qwen3.5:9b"
num_ctx = 12288
think = false
max_output_tokens = 3072
"@
$r1 = @"
provider = "ollama"
model = "deepseek-r1:14b"
num_ctx = 12288
think = true
temperature = 0.6
merge_system_prompt = true
json_schema = false      # R1 thinking + schema -> Ollama HTTP 500 (measured)
max_output_tokens = 4096
timeout_s = 900.0
"@
$qwen27 = @"
provider = "ollama"
model = "qwen3.5:27b"
num_ctx = 16384
think = true
max_output_tokens = 8192
timeout_s = 900.0
"@
$qwen4 = @"
provider = "ollama"
model = "qwen3.5:4b"
num_ctx = 12288
think = false
max_output_tokens = 3072
timeout_s = 900.0
"@
switch ($Plan) {
    "cpu"   { $mid = $qwen4; $strong = $qwen4; $strongest = $qwen4; $exclusive = "true";  $par = 1
              $why = "no usable GPU: the small Qwen3.5 4B for every agent (expect minutes per ballot)" }
    "gpu8"  { $mid = $qwen9; $strong = $qwen9; $strongest = $qwen9; $exclusive = "true";  $par = 1
              $why = "8-11 GB VRAM: Qwen3.5 9B for every agent, one model loaded" }
    "gpu12" { $mid = $qwen9; $strong = $r1;    $strongest = $r1;    $exclusive = "true";  $par = 1
              $why = "12-19 GB VRAM: Research on Qwen3.5 9B, committee on DeepSeek-R1 14B, swapped" }
    "gpu24" { $mid = $qwen9; $strong = $r1;    $strongest = $qwen27; $exclusive = "true"; $par = 1
              $why = "20-39 GB VRAM: adds Qwen3.5 27B for Neutral; models swap one at a time" }
    "gpu48" { $mid = $qwen9; $strong = $r1;    $strongest = $qwen27; $exclusive = "false"; $par = 3
              $why = "40+ GB VRAM: all three models loaded side by side, 3 calls in parallel" }
}
Write-Host "Plan: $Plan ($why)" -ForegroundColor Green

$config = @"
# Written by scripts/windows-setup.ps1 for this PC (plan: $Plan). Safe to edit;
# re-running the setup with -Plan <name> rewrites it (the old copy is kept as .bak).
# $why

[data]
source = "csv"
sec_user_agent = "env:SEC_USER_AGENT"   # set in .env.local; empty = no SEC data

[models.mid]
$mid

[models.strong]
$strong

[models.strongest]
$strongest

[runtime]
exclusive_models = $exclusive
max_concurrent_calls = $par

# Same as the lab website: once all three repeat the same ballot for 3 rounds,
# 2 of 3 pass votes close the paper (11/11 decided in the lab vs 6/11 without).
[committee]
majority_pass_after_unchanged_rounds = 3
"@
if (Test-Path "config.local.toml") { Copy-Item "config.local.toml" "config.local.toml.bak" -Force }
Write-Utf8NoBom (Join-Path $repo "config.local.toml") $config
Write-Host "Wrote config.local.toml"

# ---------------------------------------------------------------- SEC contact
if (-not (Test-Path ".env.local")) {
    Step "SEC contact (optional)"
    Write-Host "SEC filings (fundamentals, 8-K news) need a contact the SEC can reach,"
    Write-Host "for example:  Qfii yourname@example.com   (leave empty to skip SEC data)"
    $sec = Read-Host "SEC contact"
    if ($sec) {
        Write-Utf8NoBom (Join-Path $repo ".env.local") "SEC_USER_AGENT=`"$sec`"`n"
        $env:SEC_USER_AGENT = $sec
    }
} else {
    Get-Content ".env.local" | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$') {
            Set-Item "env:$($Matches[1])" $Matches[2]
        }
    }
}

# ---------------------------------------------------------------- models
Step "Downloading the models (several GB the first time)"
& ollama list *> $null
if ($LASTEXITCODE -ne 0) {
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}
$models = @()
foreach ($block in @($mid, $strong, $strongest)) {
    if ($block -match 'model = "([^"]+)"') { $models += $Matches[1] }
}
foreach ($m in ($models | Select-Object -Unique)) {
    Write-Host "ollama pull $m"
    & ollama pull $m
    if ($LASTEXITCODE -ne 0) { throw "ollama pull $m failed" }
}

# ---------------------------------------------------------------- data + check
if (-not $SkipData) {
    Step "Downloading prices (incl. SPY), FRED macro and SEC data"
    & $vpy -m qfii.cli --config config.local.toml data fetch
}
Step "Checking the configuration"
& $vpy -m qfii.cli --config config.local.toml config
if ($LASTEXITCODE -ne 0) { throw "qfii config check failed" }

# ---------------------------------------------------------------- shortcut
$start = Join-Path $repo "scripts\windows-start.ps1"
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop "Qfii.lnk"))
$link.TargetPath = "powershell.exe"
$link.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$start`""
$link.WorkingDirectory = $repo
$link.Description = "Qfii investment committee: type a ticker, watch the debate"
$link.Save()

Step "Done"
Write-Host "Start Qfii with the 'Qfii' shortcut on your desktop, or:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$start`""
Write-Host "The page opens at http://127.0.0.1:3000 - type a ticker (e.g. NVDA) and press Start debate."
