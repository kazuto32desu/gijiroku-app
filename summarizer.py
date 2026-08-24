"""claude CLI で文字起こしを議事録に構造化する。

- ライブ更新: haiku（速い・軽い）で 75 秒ごとに全文を再構造化
- 最終版: sonnet で決定事項・ネクストアクション（担当/期限）を精密に抽出
- オフライン時は (None, エラー文字列) を返す → 呼び出し側は文字起こしのみ継続
- API キー不要（インストール済みの Claude Code CLI をそのまま使う）
"""
import os
import shutil
import subprocess

import dictionary

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
LIVE_MODEL = "haiku"
FINAL_MODEL = "sonnet"

# cwd を data/ にして、親フォルダ側のプロジェクト設定を読み込ませない
RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def _terms():
    return "、".join(dictionary.terms()[:40])

LIVE_PROMPT_TEMPLATE = """あなたは会議の書記です。入力は進行中の会議のリアルタイム文字起こしです（音声認識のため誤字・脱字を含む）。
現時点までの議事録を、次の Markdown 構成でそのまま出力してください。見出しから書き始め、前置き・後書き・コードブロック囲いは一切禁止。

## いまの要約
- 箇条書き 2〜4 行で会議の現在地

## 決定事項
- 明確に決まったことだけ。まだ無ければ「- （まだありません）」

## ネクストアクション
| やること | 担当 | 期限 |
|---|---|---|
（1件も無ければ表を書かず「- （まだありません）」）

## 論点・未決
- 議論中・保留になっている項目。無ければ「- （まだありません）」

ルール:
- 音声認識の誤りは文脈から補正する。正式表記: {terms}
- 文字起こしに無い内容を推測で足さない（捏造禁止）
- 担当・期限は発言から特定できたものだけ書く。曖昧なら「（要確認）」と書く"""

FINAL_PROMPT_TEMPLATE = """あなたは一流の書記です。入力は会議の全文文字起こしです（音声認識のため誤字を含む）。
以下の Markdown 構成で最終議事録を出力してください。見出しから書き始め、前置き・後書き・コードブロック囲いは一切禁止。

# {title}

- 日時: {datetime}（所要 {duration}）

## 要約
- 3〜5 行で会議全体の要点

## 決定事項
- 決まったことを 1 行ずつ。根拠になった発言があれば括弧で補足

## ネクストアクション
| やること | 担当 | 期限 |
|---|---|---|

（担当・期限が発言から読み取れない場合は「（要確認）」。推測で埋めない）

## 議題ごとの詳細
### （議題名）
- 議論の流れと結論を簡潔に

## 保留・持ち越し
- 未決のまま終わった項目。無ければ「- なし」

ルール:
- 音声認識の誤りは文脈から補正する。正式表記: {terms}
- 文字起こしに無い内容を足さない（捏造禁止）
- 日本語で出力{speaker_rule}"""

SPEAKER_RULE = """
- 文字起こしには声質から自動判別した話者ラベル（スピーカー1 等）が付いている。発言者を踏まえて決定事項・ネクストアクションの担当を特定すること
- 会話の内容から話者の名前が確実に分かる場合のみ「スピーカー1（山田）」のように括弧で補足する。推測では書かない"""


def _run(prompt: str, stdin_text: str, model: str, timeout: int):
    if not os.path.exists(CLAUDE_BIN):
        return None, "claude CLI が見つかりません"
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", model],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=RUN_DIR,
        )
    except subprocess.TimeoutExpired:
        return None, f"タイムアウト（{timeout}秒）"
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or f"exit {r.returncode}").strip()[:300]
        if "Not logged in" in msg or "/login" in msg:
            msg = "claude CLI が未ログインです。ターミナルで claude を起動して一度ログインしてください（文字起こしは影響なし）"
        return None, msg
    out = (r.stdout or "").strip()
    if not out:
        return None, "空の応答"
    # まれに全体がコードブロックで返るのを剥がす
    if out.startswith("```"):
        out = out.strip("`").lstrip("markdown").strip()
    return out, None


def live_minutes(transcript: str, title: str):
    text = f"会議名: {title}\n\n{transcript}"
    return _run(LIVE_PROMPT_TEMPLATE.format(terms=_terms()), text, LIVE_MODEL, timeout=120)


def final_minutes(transcript: str, title: str, datetime_str: str, duration_str: str, num_speakers: int = 0):
    prompt = FINAL_PROMPT_TEMPLATE.format(
        title=title, datetime=datetime_str, duration=duration_str, terms=_terms(),
        speaker_rule=SPEAKER_RULE if num_speakers else "",
    )
    return _run(prompt, transcript, FINAL_MODEL, timeout=300)
