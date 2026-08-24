"""議事録の自己完結 HTML 生成 — 社外シェア用。

CSS 内蔵・外部依存なし・1 ファイルで完結（メール添付 / Slack 共有 / どの環境でも開ける）。
議事録本文＋折りたたみの文字起こし全文（話者色分け）を含む。デザインは DESIGN.md v2 準拠。
"""
import html
import re

_TRANSCRIPT_LINE = re.compile(r"^\[(\d{2}):(\d{2})\]\s*(?:(スピーカー\d+):\s*)?(.*)$")

SPEAKER_COLORS = ["#D51F45", "#A85700", "#8A6D1C", "#8E1530", "#6E6E6E", "#1A1A1A"]


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


def md_to_html(md: str) -> str:
    """最小限の Markdown → HTML（見出し・リスト・表・太字。フロントエンド app.js と同等ロジック）"""
    out, in_ul, in_table = [], False, False

    def close_all():
        nonlocal in_ul, in_table
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for raw in (md or "").splitlines():
        t = raw.strip()
        if re.fullmatch(r"\|.*\|", t):
            cells = [c.strip() for c in t[1:-1].split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            if not in_table:
                close_all()
                out.append(
                    "<table><thead><tr>"
                    + "".join(f"<th>{_inline(c)}</th>" for c in cells)
                    + "</tr></thead><tbody>"
                )
                in_table = True
                continue
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if t.startswith("### "):
            close_all()
            out.append(f"<h3>{_inline(t[4:])}</h3>")
        elif t.startswith("## "):
            close_all()
            out.append(f"<h2>{_inline(t[3:])}</h2>")
        elif t.startswith("# "):
            close_all()
            out.append(f"<h1>{_inline(t[2:])}</h1>")
        elif t.startswith("- ") or t.startswith("* "):
            if not in_ul:
                close_all()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(t[2:])}</li>")
        elif t == "":
            close_all()
        else:
            close_all()
            out.append(f"<p>{_inline(t)}</p>")
    close_all()
    return "\n".join(out)


def parse_transcript_md(text: str):
    """transcript.md の本文から [(ts, speaker or None, text), ...] を復元する。"""
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    lines = []
    for raw in body.splitlines():
        m = _TRANSCRIPT_LINE.match(raw.strip())
        if m and m.group(4):
            lines.append((f"{m.group(1)}:{m.group(2)}", m.group(3), m.group(4)))
    return lines


def _speaker_css():
    rules = []
    for i, c in enumerate(SPEAKER_COLORS, 1):
        rules.append(
            f".spk{i}{{color:{c};border:1px solid {c}44;background:{c}0d;}}"
        )
    return "".join(rules)


def _strip_leading_title(md: str) -> str:
    """議事録 md 先頭の「# タイトル」「- 日時:」は HTML ヘッダと重複するので取り除く。"""
    out, skipping = [], True
    for ln in (md or "").splitlines():
        t = ln.strip()
        if skipping and (t.startswith("# ") or t.startswith("- 日時:") or t == ""):
            continue
        skipping = False
        out.append(ln)
    return "\n".join(out)


def render(title: str, datetime_str: str, duration_str: str, num_speakers, minutes_md: str, transcript_lines) -> str:
    """自己完結 HTML を返す。transcript_lines: [(ts, speaker or None, text), ...]"""
    minutes_html = md_to_html(_strip_leading_title(minutes_md)) if minutes_md else "<p class='muted'>（議事録は未生成です — 文字起こしのみ）</p>"
    spk_note = f"・話者 {num_speakers} 名（自動判別）" if num_speakers else ""
    rows = []
    for ts, spk, text in transcript_lines:
        chip = ""
        if spk:
            n = re.sub(r"\D", "", spk) or "1"
            idx = (int(n) - 1) % len(SPEAKER_COLORS) + 1
            chip = f'<span class="chip spk{idx}">{html.escape(spk)}</span>'
        rows.append(
            f'<div class="seg"><span class="ts">{html.escape(ts)}</span>{chip}<span class="tx">{html.escape(text)}</span></div>'
        )
    transcript_html = "\n".join(rows) or "<p class='muted'>（発話なし）</p>"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — 議事録</title>
<style>
:root {{ --crimson:#D51F45; --orange:#EF7D00; --ink:#1A1A1A; --gray:#6E6E6E; --gray2:#9C9C98; --tint:#FCEEEC; --line:#E3E0DA; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:"Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic",Meiryo,sans-serif; color:var(--ink); background:#fff; font-size:14px; line-height:1.8; }}
.page {{ max-width:780px; margin:0 auto; padding:40px 28px 60px; }}
.kicker {{ font-family:"Helvetica Neue",Arial,sans-serif; font-size:11px; font-weight:700; letter-spacing:.22em; color:var(--gray2); text-transform:uppercase; }}
h1.doc {{ font-size:24px; font-weight:800; margin:4px 0 2px; }}
.meta {{ font-size:12.5px; color:var(--gray); margin-bottom:6px; }}
.bar {{ width:68px; height:5px; border-radius:3px; background:linear-gradient(135deg,#D51F45,#EF7D00); margin:10px 0 26px; }}
h1 {{ font-size:19px; font-weight:800; margin:18px 0 8px; }}
h2 {{ font-size:15.5px; font-weight:800; margin:22px 0 8px; }}
h2::before {{ content:"▍"; color:var(--orange); margin-right:5px; }}
h3 {{ font-size:14px; font-weight:800; margin:14px 0 4px; }}
p {{ margin:6px 0; }}
ul {{ padding-left:22px; margin:6px 0; }}
li {{ margin:4px 0; }}
table {{ border-collapse:collapse; width:100%; margin:10px 0; font-size:13px; }}
th {{ background:var(--crimson); color:#fff; font-weight:700; padding:8px 12px; text-align:left; }}
td {{ padding:8px 12px; border-bottom:1px solid var(--line); }}
.muted {{ color:var(--gray2); }}
details {{ margin-top:34px; border-top:1px solid var(--line); padding-top:18px; }}
summary {{ cursor:pointer; font-weight:800; font-size:14.5px; color:var(--ink); }}
summary:hover {{ color:var(--crimson); }}
.seg {{ display:flex; gap:9px; padding:6px 0; border-bottom:1px dashed var(--line); font-size:13px; align-items:flex-start; }}
.ts {{ font-family:"Helvetica Neue",Arial,sans-serif; font-size:11px; font-weight:700; color:var(--gray2); flex:none; padding-top:4px; }}
.chip {{ flex:none; font-size:10.5px; font-weight:700; border-radius:999px; padding:1px 9px; margin-top:2px; white-space:nowrap; }}
{_speaker_css()}
.tx {{ flex:1; }}
footer {{ margin-top:44px; font-size:10.5px; color:var(--gray); border-top:1px solid var(--line); padding-top:12px; }}
@media print {{ details {{ display:block; }} .page {{ padding:0; }} }}
</style>
</head>
<body>
<div class="page">
  <div class="kicker">Meeting Minutes</div>
  <h1 class="doc">{html.escape(title)}</h1>
  <div class="meta">{html.escape(datetime_str)}（所要 {html.escape(duration_str)}）{spk_note}</div>
  <div class="bar"></div>
  {minutes_html}
  <details>
    <summary>文字起こし全文（クリックで開閉）</summary>
    <div style="margin-top:12px">
{transcript_html}
    </div>
  </details>
  <footer>この議事録はローカル音声認識（whisper）と AI 要約で自動生成されています。話者ラベルは声質による自動判別のため、誤りを含む場合があります。</footer>
</div>
</body>
</html>
"""
