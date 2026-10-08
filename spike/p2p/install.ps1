# Create .venv next to this script and install py-libp2p (Windows).
#   powershell -ExecutionPolicy Bypass -File spike\p2p\install.ps1
#
# py-libp2p 0.8.0 supports Windows natively: its fastecdsa dependency is skipped on
# Windows, and every compiled dependency ships a Windows wheel for Python 3.10-3.13.
# Python 3.14 is NOT ok yet (coincurve and miniupnpc have no 3.14 wheels -> compiler needed).

$ErrorActionPreference = "Continue"  # native stderr must not abort (Windows PowerShell 5.1); exit codes are checked
Set-Location -Path $PSScriptRoot

function Test-Python($exe, $pre) {
    try {
        $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(1[0-3])$') { return $v }
    } catch {}
    return $null
}

# Prefer the py launcher with 3.12, then 3.13 / 3.11 / 3.10, then plain python.
$python = $null; $pyArgs = @()
foreach ($ver in @("3.12", "3.13", "3.11", "3.10")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $v = Test-Python "py" @("-$ver")
        if ($v) { $python = "py"; $pyArgs = @("-$ver"); break }
    }
}
if (-not $python) {
    foreach ($cmd in @("python", "python3")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $v = Test-Python $cmd @()
            if ($v) { $python = $cmd; break }
        }
    }
}
if (-not $python) {
    Write-Host "Need Python 3.10-3.13 (3.12 recommended). Install it with:" -ForegroundColor Red
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor Red
    Write-Host "or from https://www.python.org/downloads/ , then run this script again." -ForegroundColor Red
    exit 1
}
Write-Host "Using: $python $pyArgs  ($(& $python @pyArgs --version))"

& $python @pyArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 1 }
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip
# --prefer-binary: never try to compile when a wheel exists.
& $vpy -m pip install --prefer-binary -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install failed. If it tried to compile something, use Python 3.12 (see README)." -ForegroundColor Red
    exit 1
}
& $vpy -c "import libp2p, p2p_spike, importlib.metadata as m; print('OK: py-libp2p', m.version('libp2p'), 'installed')"
if ($LASTEXITCODE -ne 0) { exit 1 }
