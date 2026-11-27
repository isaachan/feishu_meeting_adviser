param([string]$Python = "py", [string]$Stamp = "")
$ErrorActionPreference = "Stop"
if (-not $Stamp) { $Stamp = Get-Date -Format "yyyyMMdd-HHmmss" }
$gui = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $gui
$venv = Join-Path $gui ".venv"
$pythonExe = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    & $Python -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Cannot create Python 3.12 environment" }
}
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $pythonExe -m pip install -e "$root\recorder[windows]" -e "$gui[ui]" pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

& $pythonExe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MeetingAdvisor `
    --distpath "$gui\dist\windows\MeetingAdvisor" --workpath "$gui\build" `
    --specpath "$gui\build" `
    --collect-all process_audio_capture --collect-all soundcard `
    --collect-all uiautomation --collect-all keyring `
    --hidden-import recorder.audio.windows_loopback `
    --hidden-import recorder.audio.windows_mic `
    --hidden-import recorder.audio.windows_mute `
    --add-data "$root\assets\icon.png;assets" `
    "$gui\launcher.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$builtExe = Join-Path $gui "dist\windows\MeetingAdvisor\MeetingAdvisor.exe"
$stampedExe = Join-Path $gui "dist\windows\MeetingAdvisor\MeetingAdvisor-$Stamp.exe"
Move-Item -Force $builtExe $stampedExe
Write-Host "Built: $stampedExe"
