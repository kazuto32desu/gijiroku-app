/* 議事録レコーダー — フロントエンド */
"use strict";
const $ = (id) => document.getElementById(id);
const BASE_TITLE = "議事録レコーダー";

let audioCtx = null, mediaStream = null, workletNode = null;
let sessionId = null;          // /api/start で発行された ID（音声送信に必須）
let sendQueue = [];            // Int16Array の配列
let flushTimer = null, pollTimer = null;
let uiState = "idle";          // idle | recording | busy | done | viewer
let meterPeak = 0;
let followTranscript = true;
let lastSegCount = -1, lastMinutes = "";

/* ---------- 便利 ---------- */
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
// 最小限の Markdown → HTML（見出し・リスト・表・太字のみ / 入力は必ずエスケープ）
function mdToHtml(md) {
  const lines = (md || "").split("\n");
  let html = "", inUl = false, inTable = false;
  const closeAll = () => {
    if (inUl) { html += "</ul>"; inUl = false; }
    if (inTable) { html += "</tbody></table>"; inTable = false; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const t = line.trim();
    if (/^\|.*\|$/.test(t)) {
      const cells = t.slice(1, -1).split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue; // 区切り行
      if (!inTable) {
        closeAll();
        html += "<table><thead><tr>" + cells.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
        inTable = true;
        continue;
      }
      html += "<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
      continue;
    }
    if (inTable) { html += "</tbody></table>"; inTable = false; }
    if (t.startsWith("### ")) { closeAll(); html += `<h3>${inline(t.slice(4))}</h3>`; }
    else if (t.startsWith("## ")) { closeAll(); html += `<h2>${inline(t.slice(3))}</h2>`; }
    else if (t.startsWith("# ")) { closeAll(); html += `<h1>${inline(t.slice(2))}</h1>`; }
    else if (/^[-*] /.test(t)) {
      if (!inUl) { closeAll(); html += "<ul>"; inUl = true; }
      html += `<li>${inline(t.slice(2))}</li>`;
    } else if (t === "") { closeAll(); }
    else { closeAll(); html += `<p>${inline(t)}</p>`; }
  }
  closeAll();
  return html;
  function inline(s) {
    s = esc(s);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/（まだありません）|- なし/g, '<span class="empty">$&</span>');
    return s;
  }
}
function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function show(view) {
  for (const v of ["start-view", "rec-view", "done-view", "viewer-view", "dict-view"]) {
    $(v).classList.toggle("hidden", v !== view);
  }
}
function setPill(cls, text) {
  const p = $("status-pill");
  p.className = "pill " + cls;
  p.innerHTML = cls === "rec" ? '<span class="dot"></span>' + text : text;
}

/* ---------- マイクの選択 ---------- */
// 選んだマイクは localStorage に覚える。AirPods を外した等で消えていたら既定に戻す。
const MIC_KEY = "gijiroku.micId";
let micDevices = [];

function micLabel(d, i) {
  if (d.label) return d.label;                       // 許可前はブラウザがラベルを隠す
  return `マイク ${i + 1}（許可すると名前が出ます）`;
}

async function loadMics(afterPermission) {
  const sel = $("mic-select");
  let devs = [];
  try {
    devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  } catch (e) { devs = []; }
  micDevices = devs;
  const saved = localStorage.getItem(MIC_KEY) || "";
  if (!devs.length) {
    sel.innerHTML = '<option value="">マイクが見つかりません</option>';
    $("mic-hint").textContent = "マイクが接続されているか確認してください。";
    return;
  }
  sel.innerHTML =
    '<option value="">自動（Mac の標準設定に従う）</option>' +
    devs.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(micLabel(d, i))}</option>`).join("");
  // 前回の選択が今も存在すれば復元する
  sel.value = devs.some((d) => d.deviceId === saved) ? saved : "";
  if (saved && sel.value !== saved) {
    localStorage.removeItem(MIC_KEY);
    $("mic-hint").textContent = "前回のマイクが見つからないため、自動に戻しました。";
  } else if (!devs[0].label && !afterPermission) {
    $("mic-hint").textContent = "デバイス名は、最初の録音でマイクを許可すると表示されます。";
  } else {
    $("mic-hint").textContent = "";
  }
}

function selectedMicId() {
  const v = $("mic-select").value;
  return v && micDevices.some((d) => d.deviceId === v) ? v : "";
}

function selectedMicName() {
  const v = selectedMicId();
  if (!v) return "自動（Mac の標準設定）";
  const i = micDevices.findIndex((d) => d.deviceId === v);
  return i >= 0 ? micLabel(micDevices[i], i) : "自動";
}

/* ---------- 録音 ---------- */
async function startRecording() {
  const title = $("title-input").value;
  let r;
  try {
    r = await fetch("/api/start", { method: "POST", body: JSON.stringify({ title }) });
  } catch (e) { alert("サーバーに接続できません: " + e); return; }
  if (!r.ok) { alert((await r.json()).error || "開始できませんでした"); return; }
  sessionId = (await r.json()).id;

  mediaStream = null;
  let micErr = null;
  const micId = selectedMicId();
  const audioOpts = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (micId) audioOpts.deviceId = { exact: micId };
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: audioOpts });
  } catch (e) {
    micErr = e;
    // 選んだマイクが使えないときは、自動に落として録り逃さないようにする
    if (micId) {
      try {
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        micErr = null;
      } catch (e2) { micErr = e2; }
    }
  }
  if (!mediaStream) {
    const e = micErr || new Error("マイクを開始できませんでした");
    await fetch("/api/discard", { method: "POST", headers: { "X-Session": sessionId || "" } }); // サーバー側の空セッションを破棄
    alert("マイクを使用できません。ブラウザのマイク許可を確認してください。\n" + e);
    return;
  }
  try { audioCtx = new AudioContext({ sampleRate: 16000 }); }
  catch (e) { audioCtx = new AudioContext(); }
  await audioCtx.audioWorklet.addModule("/static/worklet.js");
  const src = audioCtx.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioCtx, "pcm-tap");
  workletNode.port.onmessage = (e) => onPCM(e.data);
  src.connect(workletNode);
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  workletNode.connect(mute);
  mute.connect(audioCtx.destination);

  sendQueue = [];
  flushTimer = setInterval(flushAudio, 400);
  uiState = "recording";
  $("rec-title").textContent = title || "（無題の会議）";
  const track = mediaStream.getAudioTracks()[0];
  $("rec-mic").textContent = (track && track.label) || selectedMicName();
  loadMics(true);   // 許可が下りた直後はデバイス名が取れるようになる
  $("minutes-body").innerHTML = '<div class="wait-note">録音がたまったら、ここに要約・決定事項・ネクストアクションが自動で育っていきます。</div>';
  $("transcript-body").innerHTML = "";
  lastSegCount = -1; lastMinutes = "";
  followTranscript = true;
  setPill("rec", "録音中");
  document.title = "● 録音中 — " + BASE_TITLE;
  show("rec-view");
}

function onPCM(f32) {
  // レベルメーター（クライアント側で即時計算）
  let peak = 0;
  for (let i = 0; i < f32.length; i += 4) { const a = Math.abs(f32[i]); if (a > peak) peak = a; }
  meterPeak = Math.max(peak, meterPeak * 0.85);
  // 16kHz へリサンプル（AudioContext が 16kHz を拒否した場合のみ）
  let data = f32;
  if (audioCtx && audioCtx.sampleRate !== 16000) {
    const ratio = audioCtx.sampleRate / 16000;
    const n = Math.floor(f32.length / ratio);
    data = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const pos = i * ratio, lo = Math.floor(pos), hi = Math.min(lo + 1, f32.length - 1), frac = pos - lo;
      data[i] = f32[lo] * (1 - frac) + f32[hi] * frac;
    }
  }
  const i16 = new Int16Array(data.length);
  for (let i = 0; i < data.length; i++) {
    const v = Math.max(-1, Math.min(1, data[i]));
    i16[i] = v < 0 ? v * 32768 : v * 32767;
  }
  sendQueue.push(i16);
}

async function flushAudio() {
  if (!sendQueue.length) return;
  const chunks = sendQueue; sendQueue = [];
  let total = 0;
  for (const c of chunks) total += c.length;
  const all = new Int16Array(total);
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  try {
    await fetch("/api/audio", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Session": sessionId || "" }, body: all.buffer });
  } catch (e) { /* 次の flush でまとめて再送はしない（70点: サーバー側 raw が正） */ }
}

async function stopRecording(discard) {
  clearInterval(flushTimer); flushTimer = null;
  await flushAudio();
  try {
    if (workletNode) workletNode.disconnect();
    if (audioCtx) await audioCtx.close();
    if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
  } catch (e) {}
  audioCtx = null; mediaStream = null; workletNode = null;
  document.title = BASE_TITLE;
  if (discard) {
    await fetch("/api/discard", { method: "POST", headers: { "X-Session": sessionId || "" } });
    uiState = "idle"; setPill("idle", "待機中");
    show("start-view"); refreshIdle();
    return;
  }
  await fetch("/api/stop", { method: "POST", headers: { "X-Session": sessionId || "" } });
  uiState = "busy";
  $("busy-label").textContent = "残りの音声を文字起こし中…";
  $("busy-sub").textContent = "長い会議ほど少し時間がかかります";
  $("busy").classList.remove("hidden");
}

/* ---------- ポーリング ---------- */
async function poll() {
  let st;
  try {
    st = await (await fetch("/api/state")).json();
  } catch (e) { return; }

  if (uiState === "dict") return;
  if (uiState === "recording" && st.status === "recording") {
    $("elapsed").textContent = fmtTime(st.elapsed || 0);
    const lv = audioCtx ? meterPeak : Math.min(1, (st.level || 0) * 4); // 観戦モードはサーバー側レベル
    $("meter-fill").style.width = Math.min(100, Math.round(lv * 130)) + "%";
    $("offline-note").classList.toggle("hidden", st.online !== false);
    renderSegments(st.segments || [], "transcript-body");
    const sub = $("transcript-sub");
    if (st.backlog > 20) sub.textContent = `文字起こしが ${st.backlog} 秒ぶん追いかけ中…`;
    else sub.textContent = "約 10 秒ごとに追記されます";
    if (st.minutes_md && st.minutes_md !== lastMinutes) {
      lastMinutes = st.minutes_md;
      $("minutes-body").innerHTML = mdToHtml(st.minutes_md);
    }
    if (st.minutes_age != null) $("minutes-sub").textContent = `最終更新: ${st.minutes_age} 秒前（約 75 秒ごとに自動更新）`;
    // サーバー側ウォッチドッグで自動停止された場合
    if (st.status !== "recording") uiState = "busy";
  } else if (uiState === "busy") {
    if (st.status === "summarizing") {
      if (st.phase === "diarize") {
        $("busy-label").textContent = "話者を分析中…";
        $("busy-sub").textContent = "声質から発言者（スピーカー1, 2, …）を判別しています";
      } else {
        $("busy-label").textContent = "議事録を仕上げ中…";
        $("busy-sub").textContent = "決定事項とネクストアクションを抽出しています";
      }
    }
    if (st.status === "done") {
      $("busy").classList.add("hidden");
      uiState = "done";
      document.title = BASE_TITLE;
      setPill("ok", "完了");
      $("done-path").textContent = (st.final && st.final.dir) || "";
      $("done-spk").textContent = st.num_speakers ? `話者 ${st.num_speakers} 名を自動判別しました（S1, S2, …）` : "";
      $("done-minutes").innerHTML = st.minutes_md
        ? mdToHtml(st.minutes_md)
        : '<div class="wait-note">オフラインのため議事録は未生成です。文字起こしは保存済み — ネット復帰後に「過去の議事録」から生成できます。</div>';
      renderSegments(st.segments || [], "done-transcript");
      window._doneId = st.id;
      window._doneMinutes = st.minutes_md || "";
      show("done-view");
    }
  } else if (uiState === "recording" && st.status !== "recording") {
    // サーバー側で自動停止（タブ復帰時など）
    uiState = "busy";
    $("busy").classList.remove("hidden");
  }
}

function renderSegments(segs, targetId) {
  const el = $(targetId);
  if (segs.length === lastSegCount && targetId === "transcript-body") return;
  if (targetId === "transcript-body") lastSegCount = segs.length;
  el.innerHTML = segs
    .map((s) => {
      const chip = s.speaker ? `<span class="chip spk${((s.speaker - 1) % 6) + 1}" title="スピーカー${s.speaker}">S${s.speaker}</span>` : "";
      return `<div class="seg"><span class="ts num">${fmtTime(s.t0)}</span>${chip}<span class="tx">${esc(s.text)}</span></div>`;
    })
    .join("") || '<div class="wait-note">（まだ発話がありません）</div>';
  if (followTranscript && targetId === "transcript-body") el.scrollTop = el.scrollHeight;
}

/* ---------- 待機画面 / 履歴 ---------- */
async function refreshIdle() {
  try {
    const st = await (await fetch("/api/state")).json();
    if (st.status === "recording") {
      // 別タブで録音中 → このタブでは操作させない
      setPill("rec", "録音中（別タブ）");
      return;
    }
    const list = st.meetings || (await (await fetch("/api/meetings")).json()).meetings || [];
    renderMeetingList(list);
  } catch (e) {}
  try {
    const h = await (await fetch("/api/health")).json();
    if (h.version) $("ver").textContent = "V" + h.version;
    $("health").innerHTML = [
      h.model ? '文字起こし: ローカル whisper <span class="okc">✓</span>' : '<span class="ng">whisper モデル未配置</span>',
      h.claude ? '議事録AI: claude <span class="okc">✓</span>' : '<span class="ng">claude CLI なし</span>',
    ].join("<br>");
  } catch (e) {}
}
function renderMeetingList(list) {
  const el = $("mtg-list");
  if (!list.length) {
    el.innerHTML = '<div class="wait-note">まだ議事録はありません。最初の会議を録音してみてください。</div>';
    return;
  }
  el.innerHTML = list
    .map((m) => {
      const ds = m.duration_sec || 0;
      const dur = ds >= 60 ? `${Math.round(ds / 60)}分` : ds ? `${ds}秒` : "";
      const tag = m.has_minutes
        ? '<span class="tag ok">議事録あり</span>'
        : '<span class="tag pending">文字起こしのみ</span>';
      return `<div class="mtg" data-id="${m.id}"><span class="d num">${esc(m.datetime || m.id)}</span><span class="t">${esc(m.title || "会議")}</span><span class="len num">${dur}</span>${tag}</div>`;
    })
    .join("");
  el.querySelectorAll(".mtg").forEach((n) => n.addEventListener("click", () => openViewer(n.dataset.id)));
}

/* ---------- ビューア ---------- */
let viewerData = null, viewerTab = "minutes";
async function openViewer(id) {
  const m = await (await fetch("/api/meeting/" + id)).json();
  if (m.error) return;
  viewerData = m;
  viewerTab = m.minutes ? "minutes" : "transcript";
  $("v-title").textContent = (m.meta && m.meta.title) || "会議";
  const sec = (m.meta && m.meta.duration_sec) || 0;
  $("v-meta").textContent = `${(m.meta && m.meta.datetime) || id} ・ ${Math.round(sec / 60)}分${String(sec % 60).padStart(2, "0")}秒`;
  $("v-regen-row").classList.toggle("hidden", !!m.minutes);
  $("v-html").classList.toggle("hidden", !m.has_html);
  renderViewer();
  uiState = "viewer";
  show("viewer-view");
}
function renderViewer() {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === viewerTab));
  const src = viewerTab === "minutes" ? viewerData.minutes : viewerData.transcript;
  $("viewer-body").innerHTML = src
    ? mdToHtml(src.replace(/^---\n[\s\S]*?\n---\n/, ""))
    : '<div class="wait-note">まだ生成されていません。</div>';
}

/* ---------- コピー（clipboard API → 失敗時はサーバー側 osascript） ---------- */
async function copyText(text, doneEl) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {}
  if (!ok) {
    try { ok = (await (await fetch("/api/copy", { method: "POST", body: JSON.stringify({ text }) })).json()).ok; } catch (e) {}
  }
  if (doneEl) {
    doneEl.classList.toggle("hidden", !ok);
    if (ok) setTimeout(() => doneEl.classList.add("hidden"), 2500);
  }
  if (!ok) alert("コピーできませんでした");
}

/* ---------- イベント ---------- */
$("mic-select").addEventListener("change", () => {
  const v = $("mic-select").value;
  if (v) localStorage.setItem(MIC_KEY, v); else localStorage.removeItem(MIC_KEY);
  $("mic-hint").textContent = v ? `このマイクで録音します: ${selectedMicName()}` : "";
});
$("mic-refresh").addEventListener("click", () => loadMics(true));
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  // AirPods の接続・切断などをその場で反映する
  navigator.mediaDevices.addEventListener("devicechange", () => loadMics(true));
}
$("rec-btn").addEventListener("click", startRecording);
$("stop-btn").addEventListener("click", () => stopRecording(false));
$("discard-btn").addEventListener("click", () => stopRecording(true));
$("new-btn").addEventListener("click", () => { uiState = "idle"; setPill("idle", "待機中"); $("title-input").value = ""; show("start-view"); refreshIdle(); });
$("copy-btn").addEventListener("click", () => copyText(window._doneMinutes || "", $("copy-done")));
$("html-btn").addEventListener("click", () => fetch("/api/open", { method: "POST", body: JSON.stringify({ id: window._doneId, file: "html" }) }));
$("html-copy").addEventListener("click", () => copyHtmlFile(window._doneId, $("copy-done")));
$("reveal-btn").addEventListener("click", () => fetch("/api/reveal", { method: "POST", body: JSON.stringify({ id: window._doneId, file: "html" }) }));
$("v-back").addEventListener("click", () => { uiState = "idle"; show("start-view"); refreshIdle(); });
$("v-copy").addEventListener("click", () => {
  const src = viewerTab === "minutes" ? viewerData.minutes : viewerData.transcript;
  copyText((src || "").replace(/^---\n[\s\S]*?\n---\n/, ""), null);
});
$("v-reveal").addEventListener("click", () => fetch("/api/reveal", { method: "POST", body: JSON.stringify({ id: viewerData.id, file: viewerTab === "minutes" ? "minutes.md" : "transcript.md" }) }));

// HTML ファイル自体をクリップボードへ。Slack のメッセージ欄で ⌘V すると添付になる。
async function copyHtmlFile(id, doneEl) {
  let r = {};
  try {
    r = await (await fetch("/api/copy_file", { method: "POST", body: JSON.stringify({ id }) })).json();
  } catch (e) { r = { error: String(e) }; }
  if (r.ok) {
    if (doneEl) {
      doneEl.textContent = `コピーしました（Slack で ⌘V）`;
      doneEl.classList.remove("hidden");
      setTimeout(() => doneEl.classList.add("hidden"), 3500);
    } else {
      alert(`「${r.name}」をコピーしました。Slack のメッセージ欄で ⌘V を押すと添付できます。`);
    }
  } else {
    alert("コピーできませんでした: " + (r.error || "不明なエラー"));
  }
}
$("v-html").addEventListener("click", () => fetch("/api/open", { method: "POST", body: JSON.stringify({ id: viewerData.id, file: "html" }) }));
$("v-html-copy").addEventListener("click", () => copyHtmlFile(viewerData.id, null));
$("v-regen").addEventListener("click", async () => {
  $("v-regen").disabled = true;
  $("v-regen").textContent = "生成中…（1〜2 分）";
  const r = await (await fetch("/api/meeting/" + viewerData.id + "/summarize", { method: "POST" })).json();
  $("v-regen").disabled = false;
  $("v-regen").textContent = "議事録をいま生成する";
  if (r.ok) { openViewer(viewerData.id); } else { alert("生成に失敗しました（オフライン?）: " + (r.error || "")); }
});
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => { viewerTab = t.dataset.tab; renderViewer(); })
);
$("transcript-body").addEventListener("scroll", () => {
  const el = $("transcript-body");
  followTranscript = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
});
window.addEventListener("beforeunload", (e) => {
  if (uiState === "recording") { e.preventDefault(); e.returnValue = "録音中です。タブを閉じると録音が止まります。"; }
});


/* ---------- 用語辞書 ---------- */
let dictRows = [];   // [{yomi, text, note, comments}]

function renderDict() {
  const tb = $("dict-rows");
  tb.innerHTML = "";
  dictRows.forEach((e, i) => {
    // 分類の見出し（# --- 会社・サービス --- 等）はそのまま残して表示する
    (e.comments || []).forEach((c) => {
      const tr = document.createElement("tr");
      tr.className = "group-head";
      tr.innerHTML = `<td colspan="4">${esc(c)}</td>`;
      tb.appendChild(tr);
    });
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input data-i="${i}" data-k="yomi" value="${esc(e.yomi || "")}" placeholder="わんせく"></td>` +
      `<td><input data-i="${i}" data-k="text" value="${esc(e.text || "")}" placeholder="1sec."></td>` +
      `<td><input data-i="${i}" data-k="note" value="${esc(e.note || "")}" placeholder=""></td>` +
      `<td><button class="del-btn" data-del="${i}" title="この行を削除">×</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("input").forEach((el) =>
    el.addEventListener("input", () => { dictRows[+el.dataset.i][el.dataset.k] = el.value; })
  );
  tb.querySelectorAll(".del-btn").forEach((b) =>
    b.addEventListener("click", () => { dictRows.splice(+b.dataset.del, 1); renderDict(); })
  );
}

async function openDict() {
  try {
    const d = await (await fetch("/api/dictionary")).json();
    dictRows = d.entries || [];
    $("dict-path").textContent = d.path || "";
  } catch (e) { alert("辞書を読み込めませんでした: " + e); return; }
  renderDict();
  uiState = "dict";
  show("dict-view");
}

async function saveDict() {
  const btn = $("dict-save");
  btn.disabled = true; btn.textContent = "保存中…";
  let r = {};
  try {
    r = await (await fetch("/api/dictionary", {
      method: "POST", body: JSON.stringify({ entries: dictRows }),
    })).json();
  } catch (e) { r = { error: String(e) }; }
  btn.disabled = false; btn.textContent = "保存する";
  if (r.ok) {
    const m = $("dict-msg");
    m.textContent = `保存しました（${r.count} 語）`;
    m.classList.remove("hidden");
    setTimeout(() => m.classList.add("hidden"), 2500);
    refreshIdle();
  } else {
    alert("保存できませんでした: " + (r.error || "不明なエラー"));
  }
}

$("dict-open").addEventListener("click", openDict);
$("dict-save").addEventListener("click", saveDict);
$("dict-add").addEventListener("click", () => {
  dictRows.push({ yomi: "", text: "", note: "", comments: [] });
  renderDict();
  const inputs = $("dict-rows").querySelectorAll("input[data-k='yomi']");
  if (inputs.length) inputs[inputs.length - 1].focus();
});
$("dict-back").addEventListener("click", () => { uiState = "idle"; show("start-view"); refreshIdle(); });

/* ---------- アップデート ---------- */
async function checkUpdate() {
  let r;
  try { r = await (await fetch("/api/update/check")).json(); } catch (e) { return; }
  if (!r.ok || !r.available) return;                     // オフライン・最新なら黙って何もしない
  if (localStorage.getItem("gijiroku.skipVer") === r.latest) return;
  $("update-text").textContent = `新しい版 V${r.latest} があります（いまは V${r.current}）。録音データと設定はそのまま残ります。`;
  $("update-bar").classList.remove("hidden");
  window._latestVer = r.latest;
}

$("update-btn").addEventListener("click", async () => {
  const btn = $("update-btn");
  btn.disabled = true;
  $("update-text").textContent = "更新しています…";
  let r = {};
  try { r = await (await fetch("/api/update/apply", { method: "POST" })).json(); } catch (e) { r = { message: String(e) }; }
  if (!r.ok) {
    btn.disabled = false;
    $("update-text").textContent = "更新できませんでした: " + (r.message || "不明なエラー");
    return;
  }
  $("update-text").textContent = r.message + " — 再起動しています…";
  try { await fetch("/api/restart", { method: "POST" }); } catch (e) {}
  // サーバーが戻るのを待って読み込み直す
  const t0 = Date.now();
  const wait = setInterval(async () => {
    try {
      const h = await (await fetch("/api/health")).json();
      if (h && h.ok) { clearInterval(wait); location.reload(); }
    } catch (e) {}
    if (Date.now() - t0 > 30000) { clearInterval(wait); location.reload(); }
  }, 1200);
});
$("update-later").addEventListener("click", () => {
  if (window._latestVer) localStorage.setItem("gijiroku.skipVer", window._latestVer);
  $("update-bar").classList.add("hidden");
});

/* ---------- 起動 ---------- */
setPill("idle", "待機中");
if (new URLSearchParams(location.search).has("dict")) {
  openDict();
} else if (new URLSearchParams(location.search).has("spectate")) {
  // 観戦モード: このタブでは録音せず、進行中セッションの表示だけ行う（検証・サブ画面用）
  uiState = "recording";
  setPill("rec", "録音中");
  document.title = "● 録音中 — " + BASE_TITLE;
  show("rec-view");
} else {
  refreshIdle();
  loadMics(false);
  checkUpdate();
}
pollTimer = setInterval(poll, 1000);
