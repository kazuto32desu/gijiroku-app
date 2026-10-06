"""claude CLI で文字起こしを議事録に構造化する。

- ライブ更新: haiku（速い・軽い）で 75 秒ごとに全文を再構造化
- 最終版: sonnet で決定事項・ネクストアクション（担当/期限）を精密に抽出
- オフライン時は (None, エラー文字列) を返す → 呼び出し側は文字起こしのみ継続
- API キー不要（インストール済みの Claude Code CLI をそのまま使う）
- 担当者の取り違え対策（2026-10-06）: 参加者が分かっている会議では担当・話者名を参加者に限る。
  AI が守らなかったときに備えて、出力の「担当」列も機械的に検査し、参加者外の名前は「担当要確認」にする
"""
import os
import re
import shutil
import subprocess

import dictionary

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
LIVE_MODEL = "haiku"
FINAL_MODEL = "sonnet"
ASK = "担当要確認"

# cwd を data/ にして、親フォルダ側のプロジェクト設定を読み込ませない
RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _terms():
    """表記合わせ用の語。辞書の人名・社名は「参加者とは限らない」と明記して別枠で渡す。"""
    out = "正式表記: " + "、".join(dictionary.terms(("用語",))[:40])
    names = dictionary.terms(("人名", "社名"))[:40]
    if names:
        out += "\n- 人名・社名の正式表記（表記合わせ用。会議の参加者とは限らない）: " + "、".join(names)
    return out


def _people_rule(participants):
    if participants:
        nick = "（括弧の中は呼び名）" if any(re.search(r"[（(]", p) for p in participants) else ""
        return (
            f"- 会議の参加者: {'、'.join(participants)}{nick}\n"
            "- 担当・話者名はこの参加者の中からだけ選ぶ。参加者以外の名前（社外の人・話題に出ただけの人・社名）を担当にしない。"
            f"参加者の誰か特定できないときは担当欄に「{ASK}」と書く"
        )
    return (
        "- 参加者リストは無い。文字起こしの人名・社名は音声認識の誤り（似た音の別の名前、無音部分への差し込み）のことがある。"
        f"担当は、本人が引き受けた・指名されたと発言から確実に読み取れる場合だけ書き、それ以外は「{ASK}」と書く"
    )


FLAG_RULE = "- 「（要確認）」の付いた行は音声認識の誤り（繰り返し・差し込み）の可能性が高い。決定事項・担当・期限の根拠にしない"

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
- 音声認識の誤りは文脈から補正する。{terms}
- 文字起こしに無い内容を推測で足さない（捏造禁止）
- 担当・期限は発言から特定できたものだけ書く。曖昧なら担当は「担当要確認」、期限は「（要確認）」と書く
{people}
{flag_rule}"""

FINAL_PROMPT_TEMPLATE = """あなたは一流の書記です。入力は会議の全文文字起こしです（音声認識のため誤字を含む）。
以下の Markdown 構成で最終議事録を出力してください。見出しから書き始め、前置き・後書き・コードブロック囲いは一切禁止。

# {title}

- 日時: {datetime}（所要 {duration}）{who_line}

## 要約
- 3〜5 行で会議全体の要点

## 決定事項
- 決まったことを 1 行ずつ。根拠になった発言があれば括弧で補足

## ネクストアクション
| やること | 担当 | 期限 |
|---|---|---|

（担当が発言から読み取れない場合は「担当要確認」、期限が読み取れない場合は「（要確認）」。推測で埋めない）

## 議題ごとの詳細
### （議題名）
- 議論の流れと結論を簡潔に

## 保留・持ち越し
- 未決のまま終わった項目。無ければ「- なし」

ルール:
- 音声認識の誤りは文脈から補正する。{terms}
- 文字起こしに無い内容を足さない（捏造禁止）
{people}
{flag_rule}
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


# ---------- 参加者リストによる担当者の検査（AI の出力をそのまま信じない） ----------

_HONORIFIC = re.compile(r"(さん|先生|様|さま|氏|くん|君|ちゃん|殿)$")
_ANYONE = {"全員", "各自", "みんな", "皆", "参加者全員", "メンバー全員", "チーム", "全体"}
_SMALL_KANA = str.maketrans("ぁぃぅぇぉっゃゅょゎ", "あいうえおつやゆよわ")


def _name_key(s: str) -> str:
    """照合用に名前をそろえる: 括弧の補足・敬称・空白を外し、カタカナはひらがな、小さい字は大きい字に。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")     # 「田中（営業）」の括弧の補足は見ない
    s = re.sub(r"[\s　*_`]", "", s)
    s = _HONORIFIC.sub("", s).lower()
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s).translate(_SMALL_KANA)


def _participant_keys(participants):
    """参加者ごとの呼び方の候補。「山田（やまちゃん）」の括弧の中は呼び名として足し、
    辞書に読みがある人は読みでも照合する（例: 辞書の「ジョン＝じょん」→「ジョン」「じょん」も同じ人）。"""
    readings = [(_name_key(e.get("text")), _name_key(e.get("yomi"))) for e in dictionary.load()]
    out = []
    for p in participants:
        keys = {_name_key(p)} | {_name_key(a) for a in re.findall(r"[（(]([^）)]+)[）)]", p or "")}
        keys |= {y for t, y in readings if t and y and t in keys}
        out.append({k for k in keys if k})
    return out


def _is_participant(name: str, keysets) -> bool:
    """完全一致か、漢字の名前の頭が一致（「山田」と「山田太郎」）だけを同じ人とみなす。
    途中一致・かなの頭一致は使わない — 短い呼び名（例:「まる」）が社名の一部（例:「マルマル商事」）に一致してしまうため。"""
    k = _name_key(name)
    if not k:
        return True
    for keys in keysets:
        for q in keys:
            if k == q:
                return True
            short, long_ = sorted((k, q), key=len)
            if len(short) >= 2 and re.search(r"[一-鿿]", short) and long_.startswith(short):
                return True
    return False


def _check_cell(cell: str, keysets) -> str:
    if not cell or "要確認" in cell:
        return cell
    names = [n.strip() for n in re.split(r"[、,，/／&＆・]+", cell) if n.strip()]
    anyone = {_name_key(a) for a in _ANYONE}   # 「全員」「チーム全体」等は人名でないので通す
    ok = [n for n in names if any(a in _name_key(n) for a in anyone) or _is_participant(n, keysets)]
    bad = [n for n in names if n not in ok]
    if not bad:
        return cell
    return "、".join(ok + [f"{ASK}（{'、'.join(bad)}？）"])


def enforce_participants(md: str, participants) -> str:
    """表の「担当」列とスピーカー名の補足を参加者で検査し、参加者外の名前を「担当要確認」に置き換える。"""
    if not md or not participants:
        return md
    keysets = _participant_keys(participants)
    out, col = [], None
    for line in md.splitlines():
        t = line.strip()
        if t.startswith("|") and t.endswith("|") and len(t) > 1:
            cells = [c.strip() for c in t[1:-1].split("|")]
            if col is None:
                col = next((i for i, c in enumerate(cells) if "担当" in c), -1)   # 見出し行
            elif 0 <= col < len(cells) and not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                fixed = _check_cell(cells[col], keysets)
                if fixed != cells[col]:
                    cells[col] = fixed
                    line = "| " + " | ".join(cells) + " |"
        else:
            col = None
        out.append(line)
    md = "\n".join(out)
    # 「スピーカー1（山田）」の補足が参加者外なら補足を外す
    return re.sub(
        r"(スピーカー\d+)（([^）]{1,20})）",
        lambda m: m.group(0) if _is_participant(m.group(2), keysets) else m.group(1),
        md,
    )


def live_minutes(transcript: str, title: str, participants=None):
    text = f"会議名: {title}\n\n{transcript}"
    prompt = LIVE_PROMPT_TEMPLATE.format(
        terms=_terms(), people=_people_rule(participants), flag_rule=FLAG_RULE,
    )
    md, err = _run(prompt, text, LIVE_MODEL, timeout=120)
    return enforce_participants(md, participants), err


def final_minutes(transcript: str, title: str, datetime_str: str, duration_str: str, num_speakers: int = 0,
                  participants=None):
    speaker_rule = SPEAKER_RULE if num_speakers else ""
    if num_speakers and participants:
        speaker_rule += "。補足する名前は会議の参加者の中からだけ選ぶ"
    prompt = FINAL_PROMPT_TEMPLATE.format(
        title=title, datetime=datetime_str, duration=duration_str, terms=_terms(),
        who_line=f"\n- 参加者: {'、'.join(participants)}" if participants else "",
        people=_people_rule(participants), flag_rule=FLAG_RULE, speaker_rule=speaker_rule,
    )
    md, err = _run(prompt, transcript, FINAL_MODEL, timeout=300)
    return enforce_participants(md, participants), err
