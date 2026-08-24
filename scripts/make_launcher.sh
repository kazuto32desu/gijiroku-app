#!/bin/bash
# ランチャー「議事録レコーダー.app」を生成する。
#
# osacompile（AppleScript アプリ）は使わない。生成時にアドホック署名され、
# アイコン差し替えのために Info.plist や Assets.car をいじると署名が壊れ、
# macOS が「ダブルクリックしても無反応」という分かりにくい形で起動を拒否するため
# （2026-08-21 に実際に発生）。ここでは中身が全て見える最小構成のバンドルを自分で組む。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$APP_DIR/議事録レコーダー.app"
PORT="${GIJIROKU_PORT:-8940}"
# 使う Python。setup.sh が見つけたものを渡す。未指定なら macOS 標準を使う。
PY_BIN="${PYTHON_BIN:-/usr/bin/python3}"
VER="$(tr -d ' \n' < "$APP_DIR/VERSION" 2>/dev/null || echo 1.00)"

# アイコンは作成済みのものを使う。配布先には Pillow が無いため生成は走らせない。
# 形を変えたいときは REGEN_ICON=1 を付けて実行する（要 Pillow）。
if [ "${REGEN_ICON:-0}" = "1" ] || [ ! -f "$APP_DIR/scripts/icon.icns" ]; then
  python3 "$APP_DIR/scripts/make_icon.py" || { echo "ERROR: アイコン生成に失敗（Pillow が必要）"; exit 1; }
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$APP_DIR/scripts/icon.icns" "$APP/Contents/Resources/icon.icns"
printf 'APPL????' > "$APP/Contents/PkgInfo"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key><string>議事録レコーダー</string>
	<key>CFBundleDisplayName</key><string>議事録レコーダー</string>
	<key>CFBundleIdentifier</key><string>co.onesec.collie.gijiroku</string>
	<key>CFBundleVersion</key><string>$VER</string>
	<key>CFBundleShortVersionString</key><string>$VER</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleSignature</key><string>????</string>
	<key>CFBundleExecutable</key><string>launch</string>
	<key>CFBundleIconFile</key><string>icon</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>LSMinimumSystemVersion</key><string>11.0</string>
	<key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 実体はシェルスクリプト1枚。サーバーが動いていなければ起動し、ブラウザを開くだけ。
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
APP_DIR="$APP_DIR"
PORT="$PORT"
PY_BIN="$PY_BIN"
cd "\$APP_DIR" || exit 1
mkdir -p data
if ! /usr/bin/curl -s -m 2 "http://127.0.0.1:\$PORT/api/health" > /dev/null 2>&1; then
  GIJIROKU_PORT="\$PORT" nohup "\$PY_BIN" server.py >> data/server.log 2>&1 &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    /usr/bin/curl -s -m 2 "http://127.0.0.1:\$PORT/api/health" > /dev/null 2>&1 && break
  done
fi
/usr/bin/open "http://127.0.0.1:\$PORT"
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

# 全ての改変を終えてから署名する（署名後に中身を変えると起動が拒否される）
if command -v codesign > /dev/null 2>&1; then
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
  codesign -v "$APP" 2>/dev/null || echo "（注意: 署名の検証に失敗しましたが起動には支障ありません）"
fi

LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREG" ] && "$LSREG" -f "$APP" 2>/dev/null || true
touch "$APP"
echo "created: $APP"
