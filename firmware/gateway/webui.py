"""The settings app: one page, served by the gateway.

Built as a web app rather than a native one because it has to be both an
iPhone app and a browser page, and this is the only way to be both from a
single codebase. Safari installs it to the home screen with its own icon and
no browser chrome, so on the phone it is indistinguishable from a small
native app; on a desktop it is just a page. When a Mac is available and a
native build is wanted, it calls the same endpoints this page does.

Everything is inline -- no CDN, no build step, no npm. The gateway is a
single Python file started by a script, and a settings screen is not worth
turning that into a project with a toolchain.

The form is generated from settings_store.schema(), so adding a setting on
the server makes it appear here with no change to this file.
"""

INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Tachikoma">
<meta name="theme-color" content="#11161d">
<link rel="manifest" href="/ui/manifest.json">
<link rel="apple-touch-icon" href="/ui/icon.png">
<title>タチコマ設定</title>
<style>
  :root {
    --bg: #0f1419; --card: #1a2029; --line: #2b3441; --fg: #e7ecf3;
    --muted: #93a1b3; --accent: #4da3ff; --danger: #ff6b6b; --ok: #4ade80;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6f9; --card:#fff; --line:#dfe4ec; --fg:#141a22; --muted:#5c6b80; }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
    padding: env(safe-area-inset-top) env(safe-area-inset-right)
             calc(env(safe-area-inset-bottom) + 80px) env(safe-area-inset-left);
  }
  header {
    position: sticky; top: 0; z-index: 10; background: var(--bg);
    padding: 14px 16px 10px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 10px;
  }
  header h1 { font-size: 17px; margin: 0; flex: 1; font-weight: 600; }
  #status { font-size: 12px; color: var(--muted); }
  #status.saving { color: var(--accent); }
  #status.saved { color: var(--ok); }
  #status.error { color: var(--danger); }
  main { max-width: 680px; margin: 0 auto; padding: 12px 16px; }
  .tabs { display: flex; gap: 6px; padding: 8px 16px 0; max-width: 680px; margin: 0 auto; }
  .tabs button {
    flex: 1; padding: 9px 4px; border: 1px solid var(--line); background: var(--card);
    color: var(--muted); border-radius: 10px; font-size: 14px; cursor: pointer;
  }
  .tabs button[aria-selected="true"] { color: var(--fg); border-color: var(--accent); }
  section.group { background: var(--card); border: 1px solid var(--line);
                  border-radius: 14px; margin: 14px 0; overflow: hidden; }
  section.group > h2 { font-size: 13px; color: var(--muted); margin: 0;
                       padding: 12px 16px 6px; font-weight: 600; letter-spacing: .04em; }
  .row { padding: 12px 16px; border-top: 1px solid var(--line); }
  .row:first-of-type { border-top: none; }
  .row .top { display: flex; align-items: center; gap: 12px; }
  .row label.name { flex: 1; font-size: 15px; }
  .row .help { font-size: 12.5px; color: var(--muted); margin-top: 5px; }
  input[type=text], textarea, select, input[type=number] {
    width: 100%; margin-top: 8px; padding: 10px 12px; font-size: 16px;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--line); border-radius: 10px; font-family: inherit;
  }
  textarea { min-height: 92px; resize: vertical; }
  .switch { position: relative; width: 50px; height: 30px; flex: none; }
  .switch input { opacity: 0; width: 100%; height: 100%; margin: 0; }
  .switch span {
    position: absolute; inset: 0; background: var(--line); border-radius: 999px;
    transition: background .15s; pointer-events: none;
  }
  .switch span::after {
    content: ""; position: absolute; width: 24px; height: 24px; border-radius: 50%;
    background: #fff; top: 3px; left: 3px; transition: transform .15s;
  }
  .switch input:checked + span { background: var(--accent); }
  .switch input:checked + span::after { transform: translateX(20px); }
  .person { padding: 12px 16px; border-top: 1px solid var(--line); }
  .person:first-child { border-top: none; }
  .person .who { display: flex; align-items: center; gap: 10px; }
  .person .who b { font-size: 15px; font-weight: 600; }
  .person .meta { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .person select { width: auto; margin-top: 0; padding: 6px 10px; font-size: 14px; }
  .person .del { background: none; border: none; color: var(--danger);
                 font-size: 13px; cursor: pointer; padding: 6px 0; }
  .empty { padding: 24px 16px; color: var(--muted); font-size: 14px; text-align: center; }
  .note { font-size: 12.5px; color: var(--muted); padding: 4px 4px 20px; }
  dialog { border: 1px solid var(--line); background: var(--card); color: var(--fg);
           border-radius: 14px; padding: 20px; max-width: 320px; }
  dialog input { margin-top: 12px; }
  dialog button { margin-top: 12px; width: 100%; padding: 10px; border-radius: 10px;
                  border: none; background: var(--accent); color: #fff; font-size: 15px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<header><h1>タチコマ</h1><span id="status"></span></header>
<div class="tabs" role="tablist">
  <button role="tab" data-tab="settings" aria-selected="true">設定</button>
  <button role="tab" data-tab="people" aria-selected="false">覚えている人</button>
</div>
<main>
  <div id="settings"></div>
  <div id="people" hidden></div>
</main>

<dialog id="auth">
  <form method="dialog">
    <div>タチコマのトークンを入力してください</div>
    <input type="password" id="token" autocomplete="current-password" placeholder="DEVICE_TOKEN">
    <button value="ok">開く</button>
  </form>
</dialog>

<script>
const S = document.getElementById('status');
let token = localStorage.getItem('tachikoma_token') || '';

function setStatus(text, cls) { S.textContent = text; S.className = cls || ''; }

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Authorization': 'Bearer ' + token,
               'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (res.status === 401) { askToken(); throw new Error('unauthorized'); }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

function askToken() {
  const dialog = document.getElementById('auth');
  dialog.showModal();
  dialog.addEventListener('close', () => {
    token = document.getElementById('token').value.trim();
    localStorage.setItem('tachikoma_token', token);
    boot();
  }, { once: true });
}

// Saves are debounced and coalesced: typing in a persona box should not fire
// a request per keystroke, and a flipped switch should feel instant.
let pending = {}, timer = null;
function queueSave(key, value) {
  pending[key] = value;
  setStatus('保存中…', 'saving');
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const patch = pending; pending = {};
    try {
      await api('/v1/settings', { method: 'PUT', body: JSON.stringify(patch) });
      setStatus('保存しました', 'saved');
      setTimeout(() => setStatus(''), 1500);
    } catch (e) { setStatus('保存できません', 'error'); }
  }, 400);
}

function control(def, value) {
  const row = document.createElement('div'); row.className = 'row';
  const top = document.createElement('div'); top.className = 'top';
  const name = document.createElement('label'); name.className = 'name'; name.textContent = def.label;
  top.appendChild(name); row.appendChild(top);

  let input;
  if (def.type === 'bool') {
    const wrap = document.createElement('label'); wrap.className = 'switch';
    input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!value;
    const knob = document.createElement('span');
    wrap.append(input, knob); top.appendChild(wrap);
    input.addEventListener('change', () => queueSave(def.key, input.checked));
  } else if (def.type === 'select') {
    input = document.createElement('select');
    if (!def.options || !def.options.length) {
      const o = document.createElement('option');
      o.textContent = '（選択肢を取得できません）'; input.appendChild(o); input.disabled = true;
    } else {
      for (const opt of def.options) {
        const o = document.createElement('option');
        o.value = opt.value; o.textContent = opt.label;
        if (String(opt.value) === String(value)) o.selected = true;
        input.appendChild(o);
      }
    }
    row.appendChild(input);
    input.addEventListener('change', () => queueSave(def.key, input.value));
  } else {
    input = document.createElement(def.type === 'textarea' ? 'textarea' : 'input');
    if (def.type === 'number') { input.type = 'number';
      if (def.min !== undefined) input.min = def.min;
      if (def.max !== undefined) input.max = def.max;
      if (def.step !== undefined) input.step = def.step; }
    else if (def.type === 'text') input.type = 'text';
    if (def.placeholder) input.placeholder = def.placeholder;
    input.value = value ?? '';
    row.appendChild(input);
    input.addEventListener('input', () =>
      queueSave(def.key, def.type === 'number' ? Number(input.value) : input.value));
  }
  if (def.help) {
    const help = document.createElement('div'); help.className = 'help';
    help.textContent = def.help; row.appendChild(help);
  }
  return row;
}

async function loadSettings() {
  const { schema, values } = await api('/v1/settings');
  const host = document.getElementById('settings');
  host.textContent = '';
  for (const group of schema.groups) {
    const section = document.createElement('section'); section.className = 'group';
    const heading = document.createElement('h2'); heading.textContent = group.label;
    section.appendChild(heading);
    for (const def of group.settings) section.appendChild(control(def, values[def.key]));
    host.appendChild(section);
  }
}

const ROLE_LABEL = { master: '本人', household: '家族', colleague: '同僚',
                     guest: '来客', unknown: '不明' };

async function loadPeople() {
  const { people, roles } = await api('/v1/people');
  const host = document.getElementById('people');
  host.textContent = '';
  const section = document.createElement('section'); section.className = 'group';
  if (!people.length) {
    const empty = document.createElement('div'); empty.className = 'empty';
    empty.textContent = 'まだ誰も覚えていません。「自己紹介するね。俺の名前は◯◯です」と話しかけると登録されます。';
    section.appendChild(empty);
  }
  for (const person of people) {
    const row = document.createElement('div'); row.className = 'person';
    const who = document.createElement('div'); who.className = 'who';
    const nameEl = document.createElement('b'); nameEl.textContent = person.name || '(名前なし)';
    const spacer = document.createElement('span'); spacer.style.flex = '1';
    const roleSel = document.createElement('select');
    for (const role of roles) {
      const o = document.createElement('option');
      o.value = role; o.textContent = ROLE_LABEL[role] || role;
      if (role === person.role) o.selected = true;
      roleSel.appendChild(o);
    }
    roleSel.addEventListener('change', async () => {
      setStatus('保存中…', 'saving');
      try {
        await api('/v1/people', { method: 'POST',
          body: JSON.stringify({ id: person.id, role: roleSel.value }) });
        setStatus('保存しました', 'saved'); setTimeout(() => setStatus(''), 1500);
      } catch (e) { setStatus('保存できません', 'error'); }
    });
    who.append(nameEl, spacer, roleSel); row.appendChild(who);

    const meta = document.createElement('div'); meta.className = 'meta';
    const seen = person.last_seen
      ? new Date(person.last_seen * 1000).toLocaleString('ja-JP') : '—';
    meta.textContent = `会話 ${person.encounters} 回 ・ 声 ${person.voice_samples} / 顔 ${person.face_samples} ・ 最後 ${seen}`;
    row.appendChild(meta);

    const del = document.createElement('button'); del.className = 'del';
    del.textContent = 'この人を忘れる';
    del.addEventListener('click', async () => {
      if (!confirm(`${person.name || 'この人'} の記録（声と顔を含む）を削除します。元に戻せません。`)) return;
      await api('/v1/people', { method: 'POST',
        body: JSON.stringify({ id: person.id, delete: true }) });
      loadPeople();
    });
    row.appendChild(del);
    section.appendChild(row);
  }
  host.appendChild(section);
  const note = document.createElement('div'); note.className = 'note';
  note.textContent = '声と顔のデータはこのPCの中だけに保存され、外には送られません。';
  host.appendChild(note);
}

for (const tab of document.querySelectorAll('[role=tab]')) {
  tab.addEventListener('click', () => {
    for (const other of document.querySelectorAll('[role=tab]'))
      other.setAttribute('aria-selected', String(other === tab));
    document.getElementById('settings').hidden = tab.dataset.tab !== 'settings';
    document.getElementById('people').hidden = tab.dataset.tab !== 'people';
    if (tab.dataset.tab === 'people') loadPeople();
  });
}

async function boot() {
  if (!token) { askToken(); return; }
  try { await loadSettings(); setStatus(''); }
  catch (e) { if (e.message !== 'unauthorized') setStatus('接続できません', 'error'); }
}
boot();
</script>
</body>
</html>
"""

MANIFEST_JSON = """{
  "name": "\\u30bf\\u30c1\\u30b3\\u30de\\u8a2d\\u5b9a",
  "short_name": "\\u30bf\\u30c1\\u30b3\\u30de",
  "start_url": "/ui/",
  "display": "standalone",
  "background_color": "#0f1419",
  "theme_color": "#11161d",
  "icons": [{"src": "/ui/icon.png", "sizes": "512x512", "type": "image/png"}]
}"""


TALK_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Talk">
<meta name="theme-color" content="#11161d">
<title>タチコマと話す</title>
<style>
  :root {
    --bg: #0f1419; --card: #1a2029; --line: #2b3441; --fg: #e7ecf3;
    --muted: #93a1b3; --accent: #4da3ff; --danger: #ff6b6b; --ok: #4ade80;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6f9; --card:#fff; --line:#dfe4ec; --fg:#141a22; --muted:#5c6b80; }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:-apple-system,"Hiragino Sans",sans-serif;
         display:flex; flex-direction:column; height:100dvh; }
  header { padding:12px 16px; border-bottom:1px solid var(--line);
           display:flex; gap:10px; align-items:center; }
  header h1 { font-size:16px; margin:0; flex:1; }
  header label { font-size:12px; color:var(--muted); display:flex; gap:4px; align-items:center; }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex;
         flex-direction:column; gap:10px; }
  .line { max-width:85%; padding:10px 14px; border-radius:14px; line-height:1.5;
          white-space:pre-wrap; word-break:break-word; }
  .you { align-self:flex-end; background:var(--accent); color:#fff; }
  .bot { align-self:flex-start; background:var(--card); border:1px solid var(--line);
         font-size:18px; }
  .sys { align-self:center; color:var(--muted); font-size:12px; }
  footer { padding:12px 16px calc(12px + env(safe-area-inset-bottom));
           border-top:1px solid var(--line); }
  #ptt { width:100%; padding:18px; font-size:17px; font-weight:600;
         border:none; border-radius:14px; background:var(--accent); color:#fff;
         touch-action:none; user-select:none; -webkit-user-select:none; }
  #ptt.rec { background:var(--danger); }
  #token { width:100%; margin-top:8px; padding:8px; border-radius:8px;
           border:1px solid var(--line); background:var(--card); color:var(--fg);
           font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>タチコマと話す</h1>
  <label><input type="checkbox" id="mouth" checked>実機の口から返す</label>
</header>
<div id="log"><div class="line sys">ボタンを押している間だけ録音します</div></div>
<footer>
  <button id="ptt">押しながら話す</button>
  <input id="token" type="password" placeholder="DEVICE_TOKEN (最初の1回だけ)">
</footer>
<script>
// G2 (Even Hub) アプリの中身の先行実装。BLE の殻を被せる前に、
// マイク→/v1/transcribe→/v1/chat→文字表示、の本体をここで完成させておく。
// 録音は ScriptProcessor で PCM を集め、16kHz int16 に落として送る --
// MediaRecorder の webm/opus はゲートウェイが受けないため。
const logEl = document.getElementById("log");
const ptt = document.getElementById("ptt");
const tokenEl = document.getElementById("token");
tokenEl.value = localStorage.getItem("tachi_token") || "";
tokenEl.addEventListener("change", () => localStorage.setItem("tachi_token", tokenEl.value));
const say = (cls, text) => {
  const d = document.createElement("div");
  d.className = "line " + cls; d.textContent = text;
  logEl.appendChild(d); logEl.scrollTop = logEl.scrollHeight;
  return d;
};
const deviceId = () => document.getElementById("mouth").checked
  ? "80456B4DE03C"   // 実機の ID: 返事はタチコマの口とモーションで出る
  : "G2PREVIEW";     // 画面だけ: 実機は黙ったまま (外出先でうるさくしない)
const session = "talk-" + Math.random().toString(36).slice(2, 10);

let ctx = null, stream = null, node = null, chunks = [], capturing = false;
async function start() {
  if (!tokenEl.value) { say("sys", "先に DEVICE_TOKEN を入れてください"); return; }
  stream = await navigator.mediaDevices.getUserMedia({audio: true});
  ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  node = ctx.createScriptProcessor(4096, 1, 1);
  chunks = []; capturing = true;
  node.onaudioprocess = (e) => { if (capturing) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
  src.connect(node); node.connect(ctx.destination);
  ptt.classList.add("rec"); ptt.textContent = "離すと送信";
}
function downsampleTo16k(buffers, fromRate) {
  let total = 0; buffers.forEach(b => total += b.length);
  const all = new Float32Array(total); let off = 0;
  buffers.forEach(b => { all.set(b, off); off += b.length; });
  const ratio = fromRate / 16000, outLen = Math.floor(all.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const v = all[Math.floor(i * ratio)];
    out[i] = Math.max(-1, Math.min(1, v)) * 32767;
  }
  return out;
}
async function stop() {
  if (!capturing) return;
  capturing = false;
  ptt.classList.remove("rec"); ptt.textContent = "押しながら話す";
  const rate = ctx.sampleRate;
  node.disconnect(); stream.getTracks().forEach(t => t.stop()); ctx.close();
  const pcm = downsampleTo16k(chunks, rate);
  if (pcm.length < 16000 * 0.4) { say("sys", "短すぎました"); return; }
  const busy = say("sys", "認識中...");
  const auth = {"Authorization": "Bearer " + tokenEl.value};
  try {
    const tr = await fetch("/v1/transcribe", {method:"POST", body:pcm.buffer,
      headers:{...auth, "Content-Type":"application/octet-stream",
               "X-Device-Id":deviceId(), "X-Sample-Rate":"16000"}});
    const trBody = await tr.json();
    if (!tr.ok || !trBody.text) { busy.textContent = "聞き取れませんでした"; return; }
    busy.remove(); say("you", trBody.text);
    const thinking = say("sys", "考え中...");
    const ch = await fetch("/v1/chat", {method:"POST",
      headers:{...auth, "Content-Type":"application/json"},
      body:JSON.stringify({device_id:deviceId(), session_id:session,
                           request_id:"r-"+Date.now(), text:trBody.text})});
    const chBody = await ch.json();
    thinking.remove();
    say("bot", ch.ok && chBody.text ? chBody.text : "(返事に失敗しました)");
  } catch (err) { busy.textContent = "通信エラー: " + err.message; }
}
ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); start(); });
ptt.addEventListener("pointerup",   (e) => { e.preventDefault(); stop(); });
ptt.addEventListener("pointercancel", () => stop());
</script>
</body>
</html>
"""
