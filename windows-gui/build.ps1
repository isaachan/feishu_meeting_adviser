param([string]$Python = "py")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $PSScriptRoot ".venv"
$pythonExe = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    & $Python -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Cannot create Python 3.12 environment" }
}
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $pythonExe -m pip install -e "$root\recorder[windows]" -e "$root\macos-gui[ui]" pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

& $pythonExe -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name MeetingAdvisor `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build" `
    --specpath "$PSScriptRoot\build" `
    --collect-all process_audio_capture --collect-all soundcard `
    --collect-all uiautomation --collect-all keyring `
    --hidden-import recorder.audio.windows_loopback `
    --hidden-import recorder.audio.windows_mic `
    --hidden-import recorder.audio.windows_mute `
    "$PSScriptRoot\launcher.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built: $PSScriptRoot\dist\MeetingAdvisor\MeetingAdvisor.exe"
