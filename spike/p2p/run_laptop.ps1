# One command for the laptop (Windows), option (a) in README.md: the laptop is the
# reachable relay, and also runs a worker and a requester.
#   powershell -ExecutionPolicy Bypass -File spike\p2p\run_laptop.ps1
#
# What it does:
#   1. installs py-libp2p into spike\p2p\.venv if that is not there yet
#   2. runs ONE process with three roles:
#        relay      listening on 0.0.0.0:4001 (your router must forward TCP 4001 here,
#                   or UPnP must work - the script tries UPnP and says what happened)
#        worker     serves the test model "laptop-echo"
#        requester  waits up to 15 minutes for the pod's worker "pod-echo" and talks to it
#   3. stays up 30 minutes so the pod can also talk to the laptop's worker, then exits.
#      Ctrl+C stops it earlier. A UPnP port mapping it created is removed on exit.
param([int]$Port = 4001, [int]$WaitSeconds = 900, [int]$LifetimeSeconds = 1800)

$ErrorActionPreference = "Continue"  # native stderr must not abort (Windows PowerShell 5.1); exit codes are checked
Set-Location -Path $PSScriptRoot
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Write-Host "First run: installing py-libp2p ..." -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1")
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

$lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "This laptop's LAN address: $lan   (port-forward TCP $Port to it if UPnP fails)" -ForegroundColor Cyan
Write-Host "If Windows asks whether Python may accept incoming connections: allow it on Private networks." -ForegroundColor Cyan
Write-Host "When the relay prints its peer id, send Claude:  <your public IP>  and that peer id." -ForegroundColor Cyan
Write-Host ""

$log = Join-Path $PSScriptRoot "laptop-run.log"
& $vpy -u p2p_spike.py relay+worker+requester --host 0.0.0.0 --port $Port --upnp `
    --model laptop-echo --want pod-echo --wait $WaitSeconds --lifetime $LifetimeSeconds 2>&1 |
    Tee-Object -FilePath $log
Write-Host ""
Write-Host "Full output saved to $log" -ForegroundColor Cyan
