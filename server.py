#!/usr/bin/env python3
"""議事録レコーダー — ローカル録音 → whisper.cpp リアルタイム文字起こし → claude CLI で議事録

Python 標準ライブラリのみ（pip install 不要）。127.0.0.1:8940 で起動。
音声はブラウザから PCM(int16/16kHz/mono) で受け、audio.raw に逐次保存（クラッシュしても消えない）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dictionary
import html_export
import summarizer
import updater
import transcriber

BASE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(BASE, "venv", "bin", "python")
DIARIZE_PY = os.path.join(BASE, "diarize.py")
STATIC = os.path.join(BASE, "static")
MEETINGS = os.path.join(BASE, "data", "meetings")
PORT = int(os.environ.get("GIJIROKU_PORT", "8940"))
SR = 16000
CHUNK_SEC = 6            # 区切りの最小秒数
SNAP_SEC = 10            # この秒数たまったら、6〜10 秒の範囲の「最も静かな瞬間」で切る（発言の途中で切らない）
MAX_CHUNK_SEC = 30       # 追いつき処理時の最大チャンク
SUMMARY_INTERVAL = 75    # ライブ議事録の更新間隔（秒）
FIRST_SUMMARY_AFTER = 25 # 開始からの初回更新（秒）
WATCHDOG_SEC = 60        # 音声 POST が途絶えたら自動停止（タブ閉じ事故対策）

def _read_version():
    try:
        with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "1.00"
    except OSError:
        return "1.00"


APP_VERSION = _read_version()

os.makedirs(MEETINGS, exist_ok=True)


class Session:
    def __init__(self, title: str):
        now = datetime.now()
        self.id = now.strftime("%Y%m%d-%H%M%S")
        self.title = title.strip() or "会議"
        self.dir = os.path.join(MEETINGS, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.started_at = time.time()
        self.started_str = now.strftime("%Y-%m-%d %H:%M")
        self.lock = threading.Lock()
        self.pcm = bytearray()
        self.processed = 0            # 文字起こし済みバイト数
        self.segments = []            # [{t0, t1, text}]
        self.minutes_md = ""
        self.minutes_at = 0.0
        self.summarized_upto = 0
        self.last_summary_try = time.time() - SUMMARY_INTERVAL + FIRST_SUMMARY_AFTER
        self.online = True
        self.last_error = ""
        self.status = "recording"     # recording -> finalizing -> summarizing -> done
        self.phase = ""               # summarizing 中の内訳: diarize | minutes
        self.num_speakers = 0
        self.html_file = ""
        self.final = {}
        self.last_audio_at = time.time()
        self.level = 0.0
        self.whisper_busy = False
        self.raw_path = os.path.join(self.dir, "audio.raw")
        self.raw_file = open(self.raw_path, "ab")
        json_write(os.path.join(self.dir, "meta.json"), self.meta())

    def meta(self):
        return {
            "id": self.id,
            "title": self.title,
            "datetime": self.started_str,
            "duration_sec": round(len(self.pcm) / 2 / SR),
            "status": self.status,
            "num_speakers": self.num_speakers,
            "html_file": self.html_file,
            "pending_summary": not bool(self.minutes_md) if self.status == "done" else False,
        }

    def duration_str(self):
        sec = int(len(self.pcm) / 2 / SR)
        return f"{sec // 60}分{sec % 60:02d}秒"

    def transcript_text(self):
        lines = []
        for s in self.segments:
            spk = f"スピーカー{s['speaker']}: " if s.get("speaker") else ""
            lines.append(f"[{fmt_ts(s['t0'])}] {spk}{s['text']}")
        return "\n".join(lines)


STATE = {"session": None, "history_note": ""}
LOCK = threading.Lock()


def fmt_ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def json_write(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def find_quiet_cut(pcm: bytes, start: int, min_bytes: int, max_bytes: int) -> int:
    """start+min_bytes 〜 start+max_bytes の範囲で最も静かな 200ms の中心位置を返す（発言の途中で切らない）。"""
    win = int(0.2 * SR) * 2
    hi = min(start + max_bytes, len(pcm)) - win
    lo = start + min_bytes
    if hi <= lo:
        return min(start + max_bytes, len(pcm)) & ~1
    step = int(0.1 * SR) * 2
    best_pos, best_rms = lo, None
    for pos in range(lo, hi, step):
        r = transcriber.rms_int16(pcm[pos : pos + win])
        if best_rms is None or r < best_rms:
            best_rms, best_pos = r, pos + win // 2
    return best_pos & ~1


def transcribe_worker():
    while True:
        s = STATE["session"]
        if s and s.status in ("recording", "finalizing"):
            with s.lock:
                unproc = len(s.pcm) - s.processed
            need = SNAP_SEC * SR * 2
            if unproc >= need or (s.status == "finalizing" and unproc >= SR):
                with s.lock:
                    start = s.processed
                    limit = min(len(s.pcm) - start, MAX_CHUNK_SEC * SR * 2)
                    if unproc <= need:
                        take = limit  # 終了処理の残り（10 秒未満）は全部
                    else:
                        cut = find_quiet_cut(bytes(s.pcm[start : start + limit]), 0,
                                             CHUNK_SEC * SR * 2, SNAP_SEC * SR * 2)
                        take = min(cut, limit)
                    take -= take % 2
                    chunk = bytes(s.pcm[start : start + take])
                    s.processed = start + take
                t0 = start / (SR * 2)
                tail = s.segments[-1]["text"][-100:] if s.segments else ""
                s.whisper_busy = True
                try:
                    subsegs = transcriber.transcribe_segments(chunk, tail)
                except Exception as e:
                    subsegs = []
                    s.last_error = f"文字起こしエラー: {e}"
                s.whisper_busy = False
                if subsegs:
                    for rt0, rt1, text in subsegs:
                        s.segments.append({"t0": round(t0 + rt0, 2), "t1": round(t0 + rt1, 2), "text": text})
                    try:
                        with open(os.path.join(s.dir, "transcript.md"), "w", encoding="utf-8") as f:
                            f.write(transcript_md(s))
                    except OSError:
                        pass
                continue  # 未処理が残っている可能性があるので間を置かず次へ
            if s.status == "finalizing" and unproc < SR:
                # 最終要約は数分かかりうるので別スレッドへ（次の会議の文字起こしを塞がない）
                s.status = "summarizing"
                threading.Thread(target=finalize, args=(s,), daemon=True).start()
                continue
            # 録音中のウォッチドッグ: ブラウザからの音声が途絶えたら自動停止
            if s.status == "recording" and time.time() - s.last_audio_at > WATCHDOG_SEC:
                s.status = "finalizing"
                s.last_error = "音声が途絶えたため自動停止しました（タブが閉じられた可能性）"
                continue
        time.sleep(0.3)


def summarize_worker():
    while True:
        s = STATE["session"]
        if (
            s
            and s.status == "recording"
            and s.segments
            and len(s.segments) > s.summarized_upto
            and time.time() - s.last_summary_try >= SUMMARY_INTERVAL
        ):
            s.last_summary_try = time.time()
            upto = len(s.segments)
            md, err = summarizer.live_minutes(s.transcript_text(), s.title)
            if md:
                s.minutes_md = md
                s.minutes_at = time.time()
                s.summarized_upto = upto
                s.online = True
            else:
                s.online = False
                s.last_error = f"議事録更新スキップ: {err}"
        time.sleep(2)


def transcript_md(s: Session) -> str:
    head = (
        f"---\ntitle: 文字起こし — {s.title}\ntype: workspace\ncreated: {s.started_str[:10]}\n"
        f"status: active\nowner: gijiroku-app\n---\n\n"
        f"# 文字起こし — {s.title}\n\n- 日時: {s.started_str}（{s.duration_str()}）\n\n"
    )
    return head + s.transcript_text() + "\n"


def run_diarization(wav_path: str):
    """話者分離をサブプロセス（venv）で実行。失敗したら None（話者なしで続行）。"""
    if not (os.path.exists(VENV_PY) and os.path.exists(DIARIZE_PY)):
        return None
    try:
        r = subprocess.run(
            [VENV_PY, DIARIZE_PY, wav_path],
            capture_output=True, text=True, timeout=1800,
        )
        data = json.loads(r.stdout.strip().splitlines()[-1])
        if data.get("ok") and data.get("turns"):
            return data
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        pass
    return None


def _best_turn(turns, t0, t1):
    best, best_ov = None, 0.0
    for t in turns:
        ov = min(t1, t["end"]) - max(t0, t["start"])
        if ov > best_ov:
            best_ov, best = ov, t
    return best, best_ov


def assign_speakers(segments, turns):
    """whisper セグメントに話者を割り当てる。話者交代をまたぐセグメントは
    句読点で文に分け、文字数比で近似した時刻から各文の話者を決める。"""
    out = []
    for seg in segments:
        overlapping = [
            t for t in turns
            if min(seg["t1"], t["end"]) - max(seg["t0"], t["start"]) > 0.3
        ]
        speakers = {t["speaker"] for t in overlapping}
        parts = [p.strip() for p in re.split(r"(?<=[。．?？!！])", seg["text"]) if p.strip()]
        if len(speakers) <= 1 or len(parts) <= 1:
            best, ov = _best_turn(turns, seg["t0"], seg["t1"])
            if best and ov > 0:
                seg["speaker"] = best["speaker"]
            out.append(seg)
            continue
        dur = seg["t1"] - seg["t0"]
        total = sum(len(p) for p in parts) or 1
        cursor = seg["t0"]
        for p in parts:
            p0 = cursor
            p1 = cursor + dur * len(p) / total
            cursor = p1
            piece = {"t0": round(p0, 2), "t1": round(p1, 2), "text": p}
            best, ov = _best_turn(turns, p0, p1)
            if best and ov > 0:
                piece["speaker"] = best["speaker"]
            out.append(piece)
    segments[:] = out


def share_html_name(title: str, date_str: str) -> str:
    """共有したときに中身が分かるファイル名にする（例: 議事録_メディア教育定例_20260824.html）。"""
    safe = re.sub(r'[/:\\?%*|"<>\x00-\x1f]', "", (title or "会議")).strip()[:40] or "会議"
    return f"議事録_{safe}_{date_str}.html"


def html_path(mid: str, meta: dict = None):
    """その会議の共有用HTMLを返す（旧形式 minutes.html も拾う）。無ければ None。"""
    d = os.path.join(MEETINGS, mid)
    names = []
    if meta and meta.get("html_file"):
        names.append(meta["html_file"])
    names.append("minutes.html")
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p):
            return p
    return None


def write_html(s: Session):
    lines = [
        (fmt_ts(seg["t0"]), f"スピーカー{seg['speaker']}" if seg.get("speaker") else None, seg["text"])
        for seg in s.segments
    ]
    s.html_file = share_html_name(s.title, s.id.split("-")[0])
    hpath = os.path.join(s.dir, s.html_file)
    with open(hpath, "w", encoding="utf-8") as f:
        f.write(html_export.render(
            s.title, s.started_str, s.duration_str(), s.num_speakers, s.minutes_md, lines
        ))
    return hpath


def finalize(s: Session):
    s.status = "summarizing"
    try:
        s.raw_file.close()
    except OSError:
        pass
    # audio.raw -> audio.wav
    wav_path = os.path.join(s.dir, "audio.wav")
    try:
        with s.lock:
            pcm = bytes(s.pcm)
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        os.unlink(s.raw_path)
    except OSError:
        pass
    # 話者分離（ローカル処理・オフラインでも動く。失敗しても話者なしで続行）
    if s.segments:
        s.phase = "diarize"
        diar = run_diarization(wav_path)
        if diar:
            assign_speakers(s.segments, diar["turns"])
            s.num_speakers = diar["num_speakers"]
    # transcript.md 確定（話者付き）
    tpath = os.path.join(s.dir, "transcript.md")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(transcript_md(s))
    # 最終議事録（オンライン時のみ。オフラインなら後から「議事録を生成」で再試行できる）
    mpath = os.path.join(s.dir, "minutes.md")
    if s.segments:
        s.phase = "minutes"
        md, err = summarizer.final_minutes(
            s.transcript_text(), s.title, s.started_str, s.duration_str(), s.num_speakers
        )
        if md:
            with open(mpath, "w", encoding="utf-8") as f:
                f.write(md + "\n")
            s.minutes_md = md
            s.online = True
        else:
            s.online = False
            s.last_error = f"最終議事録の生成に失敗（オフライン?）: {err}。あとから再生成できます"
    # シェア用 HTML（議事録が未生成でも文字起こし入りで出す）
    hpath = write_html(s) if s.segments else None
    s.phase = ""
    s.status = "done"
    s.final = {
        "dir": s.dir,
        "transcript": tpath,
        "minutes": mpath if os.path.exists(mpath) else None,
        "html": hpath,
    }
    json_write(os.path.join(s.dir, "meta.json"), s.meta())


def list_meetings():
    out = []
    try:
        ids = sorted(os.listdir(MEETINGS), reverse=True)
    except OSError:
        return out
    for mid in ids:
        mpath = os.path.join(MEETINGS, mid, "meta.json")
        if not os.path.exists(mpath):
            continue
        try:
            with open(mpath, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        meta["has_minutes"] = os.path.exists(os.path.join(MEETINGS, mid, "minutes.md"))
        meta["has_html"] = html_path(mid, meta) is not None
        out.append(meta)
    return out


def read_meeting(mid: str):
    if not re.fullmatch(r"[0-9-]+", mid):
        return None
    d = os.path.join(MEETINGS, mid)
    if not os.path.isdir(d):
        return None
    out = {"id": mid}
    for key, name in (("meta", "meta.json"), ("minutes", "minutes.md"), ("transcript", "transcript.md")):
        p = os.path.join(d, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out[key] = json.load(f) if name.endswith(".json") else f.read()
    out["has_html"] = html_path(mid, out.get("meta")) is not None
    return out


def regenerate_minutes(mid: str):
    m = read_meeting(mid)
    if not m or "transcript" not in m:
        return None, "文字起こしが見つかりません"
    meta = m.get("meta", {})
    body = re.sub(r"^---\n.*?\n---\n", "", m["transcript"], flags=re.S)
    sec = meta.get("duration_sec", 0)
    dur = f"{sec // 60}分{sec % 60:02d}秒"
    n_spk = meta.get("num_speakers", 0)
    md, err = summarizer.final_minutes(
        body, meta.get("title", "会議"), meta.get("datetime", mid), dur, n_spk
    )
    if md:
        with open(os.path.join(MEETINGS, mid, "minutes.md"), "w", encoding="utf-8") as f:
            f.write(md + "\n")
        # シェア用 HTML も同時に更新
        lines = html_export.parse_transcript_md(m["transcript"])
        with open(os.path.join(MEETINGS, mid, "minutes.html"), "w", encoding="utf-8") as f:
            f.write(html_export.render(
                meta.get("title", "会議"), meta.get("datetime", mid), dur, n_spk, md, lines
            ))
    return md, err


def copy_to_clipboard(text: str) -> bool:
    """pbcopy は環境によって空振りするので osascript + 読み戻し検証で確実にコピーする。"""
    tmp = os.path.join(BASE, "data", "_clip.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'set the clipboard to (read POSIX file "{tmp}" as «class utf8»)'],
            capture_output=True, timeout=10)
        if r.returncode != 0:
            return False
        rb = subprocess.run(
            ["osascript", "-e", "get the clipboard as «class utf8»"],
            capture_output=True, text=True, timeout=10)
        return rb.returncode == 0 and rb.stdout.strip()[:200] == text.strip()[:200]
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = os.path.normpath(path[len("/static/"):])
            full = os.path.join(STATIC, name)
            if not full.startswith(STATIC) or not os.path.isfile(full):
                self.send_response(404)
                self.end_headers()
                return
            ctype = {
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            return self._file(full, ctype)
        if path == "/api/health":
            h = transcriber.health()
            h["version"] = APP_VERSION
            h["ok"] = bool(h["whisper_bin"] and h["model"])
            h["claude"] = os.path.exists(summarizer.CLAUDE_BIN)
            return self._json(h)
        if path == "/api/state":
            s = STATE["session"]
            if not s:
                return self._json({"status": "idle", "meetings": list_meetings()[:8]})
            with s.lock:
                dur = len(s.pcm) / 2 / SR
                backlog = (len(s.pcm) - s.processed) / 2 / SR
            return self._json({
                "status": s.status,
                "phase": s.phase,
                "num_speakers": s.num_speakers,
                "id": s.id,
                "title": s.title,
                "elapsed": round(dur),
                "backlog": round(backlog),
                "online": s.online,
                "level": round(s.level, 3),
                "whisper_busy": s.whisper_busy,
                "error": s.last_error,
                "segments": s.segments,
                "minutes_md": s.minutes_md,
                "minutes_age": round(time.time() - s.minutes_at) if s.minutes_at else None,
                "final": s.final,
            })
        if path == "/api/dictionary":
            return self._json({"path": dictionary.path(), "entries": dictionary.load()})
        if path == "/api/update/check":
            return self._json(updater.check())
        if path == "/api/meetings":
            return self._json({"meetings": list_meetings()})
        if path.startswith("/api/meeting/"):
            m = read_meeting(path.rsplit("/", 1)[1])
            return self._json(m or {"error": "not found"}, 200 if m else 404)
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/start":
            with LOCK:
                s = STATE["session"]
                if s and s.status not in ("done",) and s.status != "idle":
                    if s.status == "recording":
                        return self._json({"error": "すでに録音中です"}, 409)
                try:
                    data = json.loads(self._body() or b"{}")
                except ValueError:
                    data = {}
                STATE["session"] = Session(data.get("title", ""))
            return self._json({"ok": True, "id": STATE["session"].id})
        if path == "/api/audio":
            s = STATE["session"]
            if not s or s.status != "recording":
                return self._json({"error": "録音セッションがありません"}, 409)
            # 混入防止: 開きっぱなしの別タブ等、開始時に発行した ID と違う送信元は拒否する
            if self.headers.get("X-Session") != s.id:
                return self._json({"error": "セッション不一致（別タブの録音?）"}, 409)
            body = self._body()
            if body:
                with s.lock:
                    s.pcm.extend(body)
                    try:
                        s.raw_file.write(body)
                    except OSError:
                        pass
                s.last_audio_at = time.time()
                s.level = transcriber.rms_int16(body[-6400:]) / 32768.0
            return self._json({"ok": True})
        if path == "/api/stop":
            s = STATE["session"]
            if not s or s.status not in ("recording",):
                return self._json({"error": "録音中ではありません"}, 409)
            if self.headers.get("X-Session") != s.id:
                return self._json({"error": "セッション不一致"}, 409)
            s.status = "finalizing"
            return self._json({"ok": True})
        if path == "/api/discard":
            # 開始直後の誤操作用: 10 秒未満のセッションのみ破棄できる
            s = STATE["session"]
            if s and self.headers.get("X-Session") not in (s.id, None, ""):
                return self._json({"error": "セッション不一致"}, 409)
            if s and s.status == "recording" and len(s.pcm) / 2 / SR < 10:
                s.status = "done"
                try:
                    s.raw_file.close()
                    shutil.rmtree(s.dir, ignore_errors=True)
                except OSError:
                    pass
                STATE["session"] = None
                return self._json({"ok": True})
            return self._json({"error": "破棄できるのは開始10秒以内のみです"}, 409)
        if path.startswith("/api/meeting/") and path.endswith("/summarize"):
            mid = path.split("/")[3]
            md, err = regenerate_minutes(mid)
            return self._json({"ok": bool(md), "minutes": md, "error": err})
        if path in ("/api/reveal", "/api/open"):
            try:
                data = json.loads(self._body() or b"{}")
            except ValueError:
                data = {}
            mid = data.get("id", "")
            target = data.get("file") or "minutes.md"
            allowed = ("minutes.md", "transcript.md", "html", "")
            if not re.fullmatch(r"[0-9-]+", mid) or target not in allowed:
                return self._json({"error": "bad id"}, 400)
            if target == "html":
                p = html_path(mid, (read_meeting(mid) or {}).get("meta")) or os.path.join(MEETINGS, mid)
            else:
                p = os.path.join(MEETINGS, mid, target)
            if not os.path.exists(p):
                p = os.path.join(MEETINGS, mid)
            # reveal = Finder で選択表示 / open = 既定アプリで開く（HTML はブラウザ）
            cmd = ["open", "-R", p] if path == "/api/reveal" else ["open", p]
            subprocess.Popen(cmd)
            return self._json({"ok": True})
        if path == "/api/copy":
            try:
                data = json.loads(self._body() or b"{}")
            except ValueError:
                data = {}
            text = data.get("text", "")
            if not text:
                m = read_meeting(data.get("id", ""))
                text = (m or {}).get(data.get("what", "minutes"), "")
            ok = copy_to_clipboard(text) if text else False
            return self._json({"ok": ok})
        if path == "/api/dictionary":
            try:
                data = json.loads(self._body() or b"{}")
            except ValueError:
                return self._json({"error": "不正なデータです"}, 400)
            entries = data.get("entries")
            if not isinstance(entries, list) or len(entries) > 2000:
                return self._json({"error": "保存できる語は 2000 件までです"}, 400)
            try:
                saved = dictionary.save(entries, today=datetime.now().strftime("%Y-%m-%d"))
            except OSError as e:
                return self._json({"error": f"保存に失敗しました: {e}"}, 500)
            return self._json({"ok": True, "path": saved, "count": len(dictionary.terms())})
        if path == "/api/copy_file":
            try:
                data = json.loads(self._body() or b"{}")
            except ValueError:
                data = {}
            mid = data.get("id", "")
            if not re.fullmatch(r"[0-9-]+", mid):
                return self._json({"error": "bad id"}, 400)
            p = html_path(mid, (read_meeting(mid) or {}).get("meta"))
            if not p:
                return self._json({"error": "共有用HTMLが見つかりません"}, 404)
            # ファイル参照をクリップボードに載せる → Slack やメールに ⌘V でそのまま添付できる
            script = f'tell application "Finder" to set the clipboard to (POSIX file "{p}" as alias)'
            try:
                r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
                ok = r.returncode == 0
                if ok:  # 実際にファイルが載ったか読み戻して確認する
                    chk = subprocess.run(["osascript", "-e", "clipboard info"],
                                         capture_output=True, text=True, timeout=10)
                    ok = "alias" in (chk.stdout or "")
            except (subprocess.TimeoutExpired, OSError):
                ok = False
            return self._json({"ok": ok, "name": os.path.basename(p)})
        if path == "/api/update/apply":
            ok, msg = updater.apply_update()
            return self._json({"ok": ok, "message": msg})
        if path == "/api/restart":
            # プログラム入れ替え後に自分を起動し直す（ランチャー経由で新しいコードを読む）
            def _restart():
                time.sleep(0.4)
                try:
                    subprocess.Popen([sys.executable, os.path.join(BASE, "server.py")],
                                     cwd=BASE, start_new_session=True,
                                     stdout=open(os.path.join(BASE, "data", "server.log"), "a"),
                                     stderr=subprocess.STDOUT)
                except OSError:
                    pass
                os._exit(0)
            threading.Thread(target=_restart, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/quit":
            def _die():
                time.sleep(0.3)
                os._exit(0)
            threading.Thread(target=_die, daemon=True).start()
            return self._json({"ok": True})
        self.send_response(404)
        self.end_headers()


def main():
    for worker in (transcribe_worker, summarize_worker):
        threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"議事録レコーダー: http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
