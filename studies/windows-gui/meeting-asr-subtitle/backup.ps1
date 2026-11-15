param(
  [Parameter(Mandatory = $true)][string]$Tag,
  [string]$Note = ""
)

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $proj
$ts = Get-Date -Format "yyyyMMdd-HHmm"
$name = "meeting-asr-subtitle_${Tag}_${ts}.zip"
$dest = Join-Path $parent $name

$items = @(
  "API.md", "CHANGELOG.md", "HANDOFF.md", "PARAMS.md",
  "asr_engine.py", "audio_capture.py", "main.py", "process_loopback.py",
  "requirements.txt", "start.bat", "stop.bat", "static"
) | Where-Object { Test-Path (Join-Path $proj $_) }

Push-Location $proj
try {
  Compress-Archive -Path $items -DestinationPath $dest -Force
} finally {
  Pop-Location
}

$item = Get-Item $dest
$hash = (Get-FileHash $dest -Algorithm SHA256).Hash
$kb = [math]::Round($item.Length / 1KB, 1)

Write-Host ""
Write-Host "Backup : $dest"
Write-Host "Size   : $kb KB"
Write-Host "SHA256 : $hash"
Write-Host ""
Write-Host "Add this row to CHANGELOG.md:"
Write-Host ("| {0} | {1} | {2} | {3} KB | {4} | {5} |" -f `
  $Tag, (Get-Date -Format "yyyy-MM-dd HH:mm"), $name, $kb, $hash.Substring(0, 8), $Note)
