#!/bin/bash
# 文字起こしエンジン（whisper.cpp）を bin/ に同梱する。配布パッケージを作る側で実行する。
# Homebrew の whisper-cpp / ggml から実行ファイルと必要なライブラリを集め、
# 参照先を「同じフォルダの中」に書き換えて、配布先に Homebrew が無くても動くようにする。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$APP_DIR/bin"

command -v whisper-cli > /dev/null || { echo "ERROR: whisper-cli が無い。brew install whisper-cpp を先に"; exit 1; }

rm -rf "$BIN"; mkdir -p "$BIN"
cp "$(command -v whisper-cli)" "$BIN/whisper-cli"
for f in /opt/homebrew/opt/whisper-cpp/lib/libwhisper*.dylib /opt/homebrew/opt/ggml/lib/libggml*.dylib; do
  [ -f "$f" ] && cp "$f" "$BIN/"
done
cp /opt/homebrew/Cellar/ggml/*/libexec/*.so "$BIN/" 2>/dev/null || true

cd "$BIN"
# 依存を再帰的に回収しつつ、参照を @loader_path（＝同じフォルダ）へ書き換える
for _ in 1 2 3 4; do
  for f in whisper-cli *.dylib *.so; do
    otool -L "$f" 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -E "^/opt/homebrew|^@loader_path" | while read -r dep; do
      base="$(basename "$dep")"
      [ -f "$base" ] && continue
      src="$(find /opt/homebrew/opt /opt/homebrew/Cellar -name "$base" -type f 2>/dev/null | head -1)"
      [ -n "$src" ] && cp "$src" .
    done || true
  done
  for f in whisper-cli *.dylib *.so; do
    install_name_tool -id "@loader_path/$(basename "$f")" "$f" 2>/dev/null || true
    otool -L "$f" 2>/dev/null | tail -n +2 | awk '{print $1}' | grep "^/opt/homebrew" | while read -r dep; do
      install_name_tool -change "$dep" "@loader_path/$(basename "$dep")" "$f" 2>/dev/null || true
    done || true
  done
done
install_name_tool -add_rpath "@loader_path" whisper-cli 2>/dev/null || true
# 改変したら必ず署名し直す（壊れた署名は macOS が黙って実行を拒否する）
for f in whisper-cli *.dylib *.so; do codesign --force --sign - "$f" 2>/dev/null || true; done

REMAIN=$(otool -L whisper-cli | tail -n +2 | awk '{print $1}' | grep -c "^/opt/homebrew" || true)
[ "$REMAIN" -eq 0 ] || { echo "ERROR: Homebrew への参照が $REMAIN 件残っている"; exit 1; }
echo "bundled: $BIN ($(du -sh "$BIN" | cut -f1))"
