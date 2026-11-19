#!/bin/sh
# Build the macOS .app package (mirror of windows-gui/build.ps1, DESIGN §7).
# Requires Python 3.11+ and Xcode Command Line Tools (swiftc) on the build Mac.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
gui=$root/macos-gui
venv=$gui/.venv

if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -e "$root/recorder" -e "$gui[ui]" pyinstaller

# Precompile the Swift helpers into the bundle: target Macs may lack swiftc,
# and the packaged launcher points recorder at them via env overrides.
helpers=$gui/build/helpers
mkdir -p "$helpers"
for helper in sck_capture macos_mute; do
    swiftc -O -parse-as-library "$root/recorder/src/recorder/audio/$helper.swift" \
        -o "$helpers/$helper"
done

"$venv/bin/python" -m PyInstaller --noconfirm --clean --onedir --windowed \
    --name MeetingAdvisor \
    --distpath "$gui/dist" --workpath "$gui/build/pyinstaller" \
    --specpath "$gui/build" \
    --collect-all sounddevice --collect-all keyring \
    --hidden-import segno \
    --hidden-import recorder.audio.sck_capture \
    --hidden-import recorder.audio.macos_mic \
    --hidden-import recorder.audio.macos_mute \
    --add-binary "$helpers/sck_capture:." --add-binary "$helpers/macos_mute:." \
    "$gui/launcher.py"

app=$gui/dist/MeetingAdvisor.app
# The app opens the microphone in-process (sounddevice); without the usage
# key macOS kills it on the first TCC prompt.
/usr/libexec/PlistBuddy -c 'Delete :NSMicrophoneUsageDescription' \
    "$app/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c \
    'Add :NSMicrophoneUsageDescription string 会参谋采集会议音频（本机麦克风，跟随飞书静音状态）' \
    "$app/Contents/Info.plist"

arch=$(uname -m)
rm -rf "$gui/dist/MeetingAdvisor"  # raw onedir COLLECT output; the .app is self-contained
zip=$gui/dist/MeetingAdvisor-macos-$arch.zip
rm -f "$zip"
ditto -c -k --keepParent "$app" "$zip"
echo "Built: $app"
echo "Archive: $zip"
