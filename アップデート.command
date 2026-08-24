#!/bin/bash
# 既存の「議事録レコーダー」を最新版に更新する（一度きりの移行用）。
# 録音データ・用語辞書・音声モデルは残したまま、プログラムだけ入れ替えます。
# V1.03 以降はアプリの画面から更新できるので、このファイルは通常使いません。
cd "$(dirname "$0")"
echo "=============================================="
echo " 議事録レコーダー アップデート"
echo "=============================================="
echo

find_install() {
  # 「models/ がある＝セットアップ済みのフォルダ」を探す
  if [ -f "./server.py" ] && [ -d "./models" ]; then echo "$PWD"; return; fi
  local hits=()
  while IFS= read -r d; do hits+=("$d"); done < <(
    find "$HOME/Documents" "$HOME/Desktop" "$HOME/Downloads" "$HOME/Applications" "$HOME" \
      -maxdepth 3 -type d -name "*" 2>/dev/null \
      | while read -r p; do [ -f "$p/server.py" ] && [ -d "$p/models" ] && echo "$p"; done | sort -u)
  [ "${#hits[@]}" -eq 1 ] && echo "${hits[0]}"
  [ "${#hits[@]}" -gt 1 ] && printf '%s\n' "${hits[@]}" > /tmp/gijiroku_hits.txt && echo "MULTI"
}

TARGET="$(find_install)"
if [ -z "$TARGET" ]; then
  echo "❌ セットアップ済みの「議事録レコーダー」が見つかりませんでした。"
  echo "   このファイルを、いま使っている『議事録レコーダー』フォルダの中に入れてから、"
  echo "   もう一度ダブルクリックしてください。"
  echo; read -n 1 -s -r -p "何かキーを押すと閉じます"; exit 1
fi
if [ "$TARGET" = "MULTI" ]; then
  echo "⚠️ 「議事録レコーダー」が複数見つかりました:"; cat /tmp/gijiroku_hits.txt | sed 's/^/   /'
  echo "   更新したいフォルダの中にこのファイルを入れて、もう一度実行してください。"
  echo; read -n 1 -s -r -p "何かキーを押すと閉じます"; exit 1
fi

echo "対象: $TARGET"
echo "現在の版: $(cat "$TARGET/VERSION" 2>/dev/null || echo '1.01 以前')"
echo
echo "更新しています（30秒ほど）..."
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true

/usr/bin/python3 - "$TARGET" <<'PYUPD'
import io, os, shutil, sys, urllib.request, zipfile
base = sys.argv[1]
ZIP = "https://github.com/kazuto32desu/gijiroku-app/archive/refs/heads/main.zip"
FILES = {"server.py","transcriber.py","summarizer.py","html_export.py","diarize.py",
         "dictionary.py","updater.py","setup.sh","VERSION","はじめにお読みください.md"}
DIRS = {"static","scripts"}
KEEP = {"dictionary.yaml","models","venv","data","bin","_backup"}
try:
    req = urllib.request.Request(ZIP, headers={"User-Agent": "gijiroku-updater"})
    with urllib.request.urlopen(req, timeout=60) as r:
        zf = zipfile.ZipFile(io.BytesIO(r.read()))
except Exception as e:
    print(f"❌ ダウンロードに失敗しました（{type(e).__name__}）。ネット接続を確認してください。"); sys.exit(1)
root = zf.namelist()[0].split("/")[0] + "/"
backup = os.path.join(base, "_backup"); shutil.rmtree(backup, ignore_errors=True); os.makedirs(backup, exist_ok=True)
n = 0
for name in zf.namelist():
    if name.endswith("/") or not name.startswith(root): continue
    rel = name[len(root):]; top = rel.split("/")[0]
    if top in KEEP or not (rel in FILES or top in DIRS): continue
    dest = os.path.join(base, rel)
    if os.path.commonpath([os.path.abspath(dest), base]) != base: continue
    if os.path.exists(dest):
        b = os.path.join(backup, rel); os.makedirs(os.path.dirname(b), exist_ok=True); shutil.copy2(dest, b)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, "wb").write(zf.read(name))
    if rel.endswith((".sh", ".command")): os.chmod(dest, 0o755)
    n += 1
print(f"   プログラム {n} ファイルを入れ替えました（新しい版: {open(os.path.join(base,'VERSION')).read().strip()}）")
PYUPD
[ $? -ne 0 ] && { echo; read -n 1 -s -r -p "何かキーを押すと閉じます"; exit 1; }

echo "アプリを作り直しています..."
( cd "$TARGET" && ./scripts/make_launcher.sh > /dev/null 2>&1 ) || echo "   （アプリの再作成に失敗しました。setup.sh を実行してください）"
echo
echo "=============================================="
echo " 完了しました"
echo "=============================================="
echo "録音データ・用語辞書・音声モデルはそのまま残っています。"
echo "「議事録レコーダー」を起動してお使いください。"
echo "次回からは、アプリの画面に出る「いま更新する」ボタンで更新できます。"
echo
read -n 1 -s -r -p "何かキーを押すと閉じます"
