"""アプリ内アップデート — GitHub の公開リポジトリから最新のプログラムだけを取り込む。

置き換えるのはプログラム（.py / static / scripts）だけ。
録音データ・音声モデル・venv・用語辞書には触れない。
失敗しても元に戻せるよう、置き換え前のファイルを _backup/ に退避する。
"""
import io
import json
import os
import shutil
import ssl
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("GIJIROKU_REPO", "kazuto32desu/gijiroku-app")
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
TIMEOUT = 30

# 更新で入れ替える対象。ここに無いもの（models/ venv/ data/ dictionary.yaml bin/）は必ず残す。
UPDATE_FILES = {
    "server.py", "transcriber.py", "summarizer.py", "html_export.py",
    "diarize.py", "dictionary.py", "updater.py", "setup.sh", "VERSION",
    "はじめにお読みください.md",
}
UPDATE_DIRS = {"static", "scripts"}
KEEP_ALWAYS = {"dictionary.yaml", "models", "venv", "data", "bin", "_backup"}


def _get(url: str, binary: bool = False):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "gijiroku-app"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        data = r.read()
    return data if binary else data.decode("utf-8").strip()


def current_version() -> str:
    try:
        with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.00"


def _as_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def check():
    """最新版の有無を調べる。ネットが無ければ available=False で静かに返す。"""
    cur = current_version()
    try:
        latest = _get(VERSION_URL)
    except Exception as e:
        return {"ok": False, "current": cur, "error": f"確認できませんでした（{type(e).__name__}）"}
    if not latest or len(latest) > 20:
        return {"ok": False, "current": cur, "error": "配布元の応答が不正です"}
    return {
        "ok": True,
        "current": cur,
        "latest": latest,
        "available": _as_tuple(latest) > _as_tuple(cur),
    }


def apply_update():
    """最新のプログラムを取り込む。成功したら要再起動。"""
    info = check()
    if not info.get("ok"):
        return False, info.get("error", "確認に失敗しました")
    if not info.get("available"):
        return False, "すでに最新です"
    try:
        blob = _get(ZIP_URL, binary=True)
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as e:
        return False, f"ダウンロードに失敗しました（{type(e).__name__}）"

    names = zf.namelist()
    if not names:
        return False, "配布物が空です"
    root = names[0].split("/")[0] + "/"

    backup = os.path.join(BASE, "_backup")
    shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(backup, exist_ok=True)

    written = 0
    for n in names:
        if n.endswith("/") or not n.startswith(root):
            continue
        rel = n[len(root):]
        top = rel.split("/")[0]
        if top in KEEP_ALWAYS:
            continue
        if not (rel in UPDATE_FILES or top in UPDATE_DIRS):
            continue
        dest = os.path.join(BASE, rel)
        if os.path.commonpath([os.path.abspath(dest), BASE]) != BASE:
            continue                                    # zip 内の細工でBASE外に書かせない
        if os.path.exists(dest):                        # 失敗時に戻せるよう退避
            b = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dest, b)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(zf.read(n))
        if rel.endswith(".sh") or rel.endswith(".command"):
            os.chmod(dest, 0o755)
        written += 1
    if written == 0:
        return False, "更新対象のファイルが見つかりませんでした"
    return True, f"{info['latest']} に更新しました（{written} ファイル）"
