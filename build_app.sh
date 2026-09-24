#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

APP_NAME="ZuckerMixer"
APP_VERSION="2.0.21"
APP_BUNDLE="$ROOT/dist/${APP_NAME}.app"
DMG_ROOT="$ROOT/dist/dmg_root"
DMG_PATH="$ROOT/dist/${APP_NAME}.dmg"
LOGO_SRC="$ROOT/static/zucker_logo_orange.png"
ICONSET="$ROOT/build/zucker.iconset"
ICNS="$ROOT/zucker.icns"
DMG_BG="$ROOT/build/dmg_background.png"
BUILD_ENV="$ROOT/.buildenv"
BUILD_METADATA="$ROOT/build/build_metadata.json"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

DMG_ONLY=0
if [[ "${1:-}" == "--dmg-only" ]]; then
  DMG_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  die "Unknown argument. Use --dmg-only to reuse the existing app bundle."
fi

echo "== ZuckerMixer macOS build =="

if [[ "$DMG_ONLY" -eq 1 ]]; then
  SKIP_APP_BUILD=1
  [[ -d "$APP_BUNDLE" ]] || die "--dmg-only requested, but no app bundle exists at $APP_BUNDLE"
  echo "Reusing existing app bundle: $APP_BUNDLE"
else
  SKIP_APP_BUILD=0
  echo "A full PyInstaller rebuild will be performed."
fi

is_framework_python() {
  local py="$1"
  [[ -x "$py" ]] || return 1
  "$py" - <<'PY' >/dev/null 2>&1
import sys, sysconfig
ok = bool(sysconfig.get_config_var("PYTHONFRAMEWORK")) or "Python.framework" in sys.executable
raise SystemExit(0 if ok else 1)
PY
}

find_framework_python() {
  local candidates=()
  local versions_dir="/Library/Frameworks/Python.framework/Versions"
  local version
  if [[ -d "$versions_dir" ]]; then
    # Matchering/Numba support is currently most reliable on Python 3.12.
    # Prefer the framework install explicitly, while retaining newer versions
    # as a fallback for builds that do not use reference mastering.
    candidates+=("$versions_dir/3.12/bin/python3")
    while IFS= read -r version; do
      candidates+=("$versions_dir/$version/bin/python3")
    done < <(
      find "$versions_dir" -maxdepth 1 -mindepth 1 -type d -exec basename {} \; \
        | awk '/^3\.[0-9]+$/ { print }' \
        | sort -Vr
    )
    candidates+=("$versions_dir/Current/bin/python3")
  fi
  candidates+=(
    "/opt/homebrew/opt/python-tk/bin/python3"
    "/usr/local/opt/python-tk/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
  )
  local py
  for py in "${candidates[@]}"; do
    if is_framework_python "$py"; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

if [[ ! -f "$LOGO_SRC" ]]; then
  DESKTOP_LOGO="$HOME/Desktop/ChatGPT Image 17 jul 2026, 00_04_53.png"
  ALT_DESKTOP_LOGO="$HOME/Desktop/ChatGPT_Image_17_jul_2026__00_04_53.png"
  if [[ -f "$DESKTOP_LOGO" ]]; then
    mkdir -p "$ROOT/static"
    cp "$DESKTOP_LOGO" "$LOGO_SRC"
  elif [[ -f "$ALT_DESKTOP_LOGO" ]]; then
    mkdir -p "$ROOT/static"
    cp "$ALT_DESKTOP_LOGO" "$LOGO_SRC"
  else
    echo "Missing logo. Expected $LOGO_SRC or the ZuckerSession PNG on Desktop." >&2
    exit 1
  fi
fi

if [[ "$SKIP_APP_BUILD" -eq 0 ]]; then
  FRAMEWORK_PYTHON="$(find_framework_python || true)"
  if [[ -z "$FRAMEWORK_PYTHON" ]]; then
    cat >&2 <<'EOF'
ERROR: No framework Python was found.

PyInstaller on macOS needs a framework Python. Your current Homebrew Python
appears to be a static build, so it cannot produce a .app bundle.

Install Python from python.org:
  1. Open https://www.python.org/downloads/macos/
  2. Download the macOS 64-bit universal2 installer
  3. Install it
  4. Re-run:
       cd /path/to/ZuckerMixer
       bash build_app.sh

This script scans:
  /Library/Frameworks/Python.framework/Versions/*/bin/python3
EOF
    exit 1
  fi

  echo "Using framework Python: $FRAMEWORK_PYTHON"

  echo "Creating isolated build virtualenv: $BUILD_ENV"
  rm -rf "$BUILD_ENV"
  "$FRAMEWORK_PYTHON" -m venv "$BUILD_ENV" || die "Could not create build virtualenv with $FRAMEWORK_PYTHON"
  BUILD_PY="$BUILD_ENV/bin/python"
  BUILD_PIP="$BUILD_ENV/bin/pip"
  BUILD_PYINSTALLER="$BUILD_ENV/bin/pyinstaller"

  [[ -x "$BUILD_PY" ]] || die "Virtualenv Python not found at $BUILD_PY"

  echo "Installing build requirements into .buildenv"
  "$BUILD_PY" -m pip install --upgrade pip setuptools wheel || die "Could not upgrade pip in .buildenv"
  "$BUILD_PIP" install --upgrade pyinstaller pywebview Flask numpy scipy soundfile pyloudnorm pillow || die "Could not install build requirements"
  # On Intel macOS, the newest numba currently resolves llvmlite to a source
  # distribution. Pin the last compatible CPython 3.12 binary pair so a clean
  # build does not require a separately installed LLVM toolchain.
  if ! "$BUILD_PIP" install --upgrade "numba==0.60.0" "llvmlite==0.43.0" matchering; then
    echo "WARNING: Matchering dependencies are unavailable; reference mastering will be disabled in this build." >&2
  fi
  "$BUILD_PYINSTALLER" --version >/dev/null || die "PyInstaller did not install correctly in .buildenv"

  BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  SOURCE_REVISION="nogit"
  if git rev-parse --show-toplevel >/dev/null 2>&1; then
    SOURCE_REVISION="$(git rev-parse HEAD)"
    if ! git diff --quiet -- . ':!build' ':!dist'; then
      SOURCE_REVISION+="-dirty"
    fi
  else
    SOURCE_REVISION="$($BUILD_PY - <<'PY'
import hashlib
from pathlib import Path

root = Path.cwd()
files = sorted(
    path
    for path in root.rglob("*")
    if path.is_file()
    and ".buildenv" not in path.parts
    and "build" not in path.parts
    and "dist" not in path.parts
    and "__pycache__" not in path.parts
)
digest = hashlib.sha256()
for path in files:
    digest.update(str(path.relative_to(root)).encode("utf-8"))
    digest.update(path.read_bytes())
print("source-" + digest.hexdigest()[:12])
PY
    )"
  fi
  export ZUCKER_BUILD_TIMESTAMP="$BUILD_TIMESTAMP"
  export ZUCKER_SOURCE_REVISION="$SOURCE_REVISION"
  export ZUCKER_APP_VERSION="$APP_VERSION"
"$BUILD_PY" - "$BUILD_TIMESTAMP" "$SOURCE_REVISION" "$BUILD_METADATA" <<'PY'
import json
import os
import sys
from pathlib import Path

_, timestamp, revision, output = sys.argv
path = Path(output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps(
        {
            "app_version": os.environ.get("ZUCKER_APP_VERSION", "development"),
            "build_timestamp": timestamp,
            "source_revision": revision,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
  echo "Build metadata: timestamp=$BUILD_TIMESTAMP revision=$SOURCE_REVISION"

  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  "$BUILD_PY" - <<'PY'
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path.cwd()
src = root / "static" / "zucker_logo_orange.png"
iconset = root / "build" / "zucker.iconset"
img = Image.open(src).convert("RGBA")
sizes = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]
for size, name in sizes:
    out = img.resize((size, size), Image.Resampling.LANCZOS)
    out.save(iconset / name)

bg = Image.new("RGB", (720, 420), "#151311")
draw = ImageDraw.Draw(bg)
logo = img.resize((120, 120), Image.Resampling.LANCZOS)
bg.paste(logo, (40, 46), logo)
try:
    title_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 30)
    text_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
except Exception:
    title_font = text_font = None
draw.text((190, 55), "ZuckerMixer", fill="#f4ead7", font=title_font)
lines = [
    "Drag the app to Applications.",
    "First launch: right-click ZuckerMixer.app, then Open.",
    "If MP3 mixing says ffmpeg is missing:",
    "brew install ffmpeg",
]
y = 112
for line in lines:
    draw.text((190, y), line, fill="#f4ead7" if "brew" not in line else "#c86f2f", font=text_font)
    y += 34
bg.save(root / "build" / "dmg_background.png")
PY

  iconutil -c icns "$ICONSET" -o "$ICNS" || die "Could not create $ICNS"
  [[ -f "$ICNS" ]] || die "Icon file was not created: $ICNS"
fi

if [[ "$SKIP_APP_BUILD" -eq 0 ]]; then
  rm -rf "$ROOT/build/pyinstaller" "$ROOT/dist"
  "$BUILD_PYINSTALLER" --noconfirm --clean --workpath "$ROOT/build/pyinstaller" --distpath "$ROOT/dist" "$ROOT/zucker_mixer.spec" || die "PyInstaller build failed"

  if [[ ! -d "$APP_BUNDLE" ]]; then
    die "Build failed: $APP_BUNDLE was not created"
  fi

  echo "Built app bundle in project: $APP_BUNDLE"
else
  [[ -d "$APP_BUNDLE" ]] || die "Existing app bundle disappeared: $APP_BUNDLE"
  [[ -f "$ICNS" ]] || die "Missing icon file for DMG: $ICNS"
  [[ -f "$DMG_BG" ]] || die "Missing DMG background file: $DMG_BG"
fi

rm -rf "$DMG_ROOT" "$DMG_PATH"
mkdir -p "$DMG_ROOT"
cp -R "$APP_BUNDLE" "$DMG_ROOT/${APP_NAME}.app" || die "Could not copy app into DMG staging folder"
cp "$ROOT/README_Zucker_Mixer_App.md" "$DMG_ROOT/READ ME FIRST.txt" || die "Could not copy README into DMG"
cp "$ICNS" "$DMG_ROOT/.VolumeIcon.icns" || die "Could not copy volume icon into DMG"
mkdir -p "$DMG_ROOT/.background"
cp "$DMG_BG" "$DMG_ROOT/.background/background.png" || die "Could not copy DMG background"

if command -v create-dmg >/dev/null 2>&1; then
  rm -f "$DMG_ROOT/Applications"
  create-dmg \
    --volname "$APP_NAME" \
    --volicon "$ICNS" \
    --background "$DMG_BG" \
    --window-pos 200 120 \
    --window-size 720 420 \
    --icon-size 96 \
    --icon "${APP_NAME}.app" 185 230 \
    --app-drop-link 520 230 \
    --hide-extension "${APP_NAME}.app" \
    "$DMG_PATH" \
    "$DMG_ROOT" || die "create-dmg failed"
else
  rm -f "$DMG_ROOT/Applications"
  ln -s /Applications "$DMG_ROOT/Applications" || die "Could not create Applications symlink"
  SetFile -a C "$DMG_ROOT" 2>/dev/null || true
  hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_ROOT" \
    -ov \
    -format UDZO \
    "$DMG_PATH" || die "hdiutil DMG creation failed"
fi

[[ -f "$DMG_PATH" ]] || die "DMG was not created: $DMG_PATH"

echo
echo "Done:"
echo "  App bundle (project): $APP_BUNDLE"
echo "  App copy: $APP_BUNDLE"
echo "  DMG: $DMG_PATH"
echo
echo "Unsigned app note: on first launch, right-click ZuckerMixer.app and choose Open."
