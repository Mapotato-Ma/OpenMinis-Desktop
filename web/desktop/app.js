/* ==========================================================================
   OpenMinis Desktop — front-end
   Talks to the same kernel API the upstream mobile UI uses:
     REST  /api/chats/*, /api/fs/*, /api/skills, /api/system/*, /api/desktop/info
     WS    /ws  ->  {type:'chat'|'shell'|'stop'|'ping'}
             <-  delta | toolStart | toolEnd | chatSession | done | usage | error
   No build step, no framework: one file, so the exe can ship it as-is.
   ========================================================================== */
'use strict';

const API = '/api';
const WS_URL = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`;

const state = {
  sessions: [],
  sessionId: null,
  messages: [],
  ws: null,
  wsReady: false,
  wsRetries: 0,
  streaming: false,
  turn: null,          // { root, textBlock, text }
  palette: { open: false, items: [], sel: 0 },
  termHistory: [],
  termIdx: -1,
  termBusy: false,
  currentFile: null,
  changes: [],         // file edits seen this session, for the diff tab
  filter: '',
  info: null,
};

const $ = (id) => document.getElementById(id);

// The empty-state block is *moved* in and out of the transcript (it is a
// sibling of the message list, not a child that gets rebuilt). Once it has
// been detached, getElementById can no longer find it, so grab the node at
// parse time — this script tag sits after it in the document — and always
// re-append this reference.
let _emptyNode = document.getElementById('emptyState');
const emptyNode = () => {
  if (!_emptyNode) _emptyNode = document.getElementById('emptyState');
  return _emptyNode;
};

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/* ── toasts ──────────────────────────────────────────────────────────── */
function toast(msg, kind) {
  const wrap = $('toastWrap');
  const t = el('div', 'toast' + (kind === 'err' ? ' err' : ''), msg);
  wrap.appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .2s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 220);
  }, kind === 'err' ? 5200 : 2600);
}

/* ── REST helper ─────────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : (typeof data === 'string' ? data : res.statusText);
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

/* ── formatting helpers ──────────────────────────────────────────────── */
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function relTime(ts) {
  if (ts == null || ts === '') return '';
  // The kernel hands back epoch milliseconds for sessions but ISO strings for
  // some other endpoints, so accept both instead of trusting one shape.
  let t = typeof ts === 'number' ? ts : (/^\d+$/.test(String(ts)) ? Number(ts) : Date.parse(ts));
  if (!t || Number.isNaN(t)) return '';
  if (t < 1e11) t *= 1000; // seconds -> ms
  const d = Date.now() - t;
  if (d < 0) return '刚刚';
  if (d < 60e3) return '刚刚';
  if (d < 3600e3) return Math.floor(d / 60e3) + ' 分钟前';
  if (d < 86400e3) return Math.floor(d / 3600e3) + ' 小时前';
  if (d < 7 * 86400e3) return Math.floor(d / 86400e3) + ' 天前';
  const dt = new Date(t);
  return `${dt.getMonth() + 1}/${dt.getDate()}`;
}

const fmtBytes = (n) => {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' K';
  return (n / 1024 / 1024).toFixed(1) + ' M';
};

function shortPath(p, max = 46) {
  if (!p) return '';
  const s = String(p).replace(/\\/g, '/');
  return s.length <= max ? s : '…' + s.slice(-(max - 1));
}

/* ── minimal markdown ────────────────────────────────────────────────── */
function renderMarkdown(src) {
  if (!src) return '';
  const fences = [];
  // Pull fenced code out first so nothing inside it gets inline-processed.
  let text = String(src).replace(/```([\w+-]*)\n?([\s\S]*?)(?:```|$)/g, (_m, lang, code) => {
    fences.push({ lang: (lang || '').trim(), code });
    return `\u0000FENCE${fences.length - 1}\u0000`;
  });

  text = esc(text);

  text = text
    .replace(/^######\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^#####\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^####\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
    .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>')
    .replace(/^\s*([-*_])\s*\1\s*\1[-*_\s]*$/gm, '<hr>')
    .replace(/^\s*&gt;\s?(.*)$/gm, '<blockquote>$1</blockquote>');

  // lists
  text = text.replace(/(?:^|\n)((?:\s*(?:[-*+]|\d+\.)\s+.*(?:\n|$))+)/g, (block) => {
    const lines = block.replace(/^\n/, '').split('\n').filter((l) => l.trim());
    const ordered = /^\s*\d+\./.test(lines[0] || '');
    const items = lines.map((l) => l.replace(/^\s*(?:[-*+]|\d+\.)\s+/, '').trim());
    return `\n<${ordered ? 'ol' : 'ul'}>` + items.map((i) => `<li>${i}</li>`).join('') + `</${ordered ? 'ol' : 'ul'}>\n`;
  });

  // inline
  text = text
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, '$1<em>$2</em>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');

  // paragraphs
  text = text.split(/\n{2,}/).map((chunk) => {
    const c = chunk.trim();
    if (!c) return '';
    if (/^<(h1|h2|h3|ul|ol|pre|blockquote|hr|table)/.test(c)) return c;
    return '<p>' + c.replace(/\n/g, '<br>') + '</p>';
  }).join('');

  return text.replace(/\u0000FENCE(\d+)\u0000/g, (_m, i) => {
    const f = fences[+i];
    if (!f) return '';
    return `<pre><code>${highlight(f.code.replace(/\n$/, ''), f.lang)}</code></pre>`;
  });
}

/* ── syntax highlighting (small, dependency-free) ────────────────────── */
const KW = {
  py: 'def class return if elif else for while import from as with try except finally raise yield lambda pass break continue global nonlocal assert del in is not and or None True False async await self match case',
  js: 'function const let var return if else for while import from export default class extends new try catch finally throw typeof instanceof await async yield of in delete void this super static get set null undefined true false break continue switch case do',
  ts: 'function const let var return if else for while import from export default class extends implements interface type enum new try catch finally throw typeof instanceof await async yield of in delete void this super static readonly public private protected null undefined true false break continue switch case do as satisfies',
  sh: 'if then else elif fi for while do done case esac function return local export readonly source set unset echo cd pwd ls cp mv rm mkdir rmdir cat grep sed awk curl wget git python3 pip npm sudo apk apt systemctl docker chmod chown find xargs printf test exit',
  go: 'package import func var const type struct interface return if else for range switch case default go defer chan map make new nil true false break continue',
  rust: 'fn let mut const static struct enum impl trait return if else match for while loop in use pub mod crate self super as where async await move ref dyn box true false break continue',
  c: 'int char float double void long short unsigned signed struct union enum typedef static const return if else for while do switch case default break continue sizeof include define ifndef endif',
};
const TYPE_RE = /\b(string|String|number|Number|boolean|Boolean|any|unknown|never|void|list|dict|List|Dict|Optional|int|str|bool|bytes|float|self|Promise|Array|Object|Map|Set)\b/g;

function highlight(code, lang) {
  const l = (lang || '').toLowerCase();
  const kset = new Set((KW[l] || KW.js).split(' '));
  const hashComment = ['py', 'sh', 'bash', 'yaml', 'yml', 'toml', 'ini', 'conf', 'r', 'rb'].includes(l);
  // One pass, alternation ordered so comments and strings win over words.
  const re = new RegExp(
    [
      '/\\*[\\s\\S]*?\\*/',
      '//[^\\n]*',
      hashComment ? '#[^\\n]*' : '(?!x)x',
      '"(?:\\\\.|[^"\\\\\\n])*"',
      "'(?:\\\\.|[^'\\\\\\n])*'",
      '`(?:\\\\.|[^`\\\\])*`',
      '\\b\\d+(?:\\.\\d+)?\\b',
      '[A-Za-z_$][\\w$]*',
    ].join('|'),
    'g',
  );
  let out = '';
  let last = 0;
  let m;
  while ((m = re.exec(code)) !== null) {
    out += esc(code.slice(last, m.index));
    const tok = m[0];
    let cls = '';
    if (tok.startsWith('/*') || tok.startsWith('//') || (hashComment && tok.startsWith('#'))) cls = 'tok-com';
    else if (tok[0] === '"' || tok[0] === "'" || tok[0] === '`') cls = 'tok-str';
    else if (/^\d/.test(tok)) cls = 'tok-num';
    else if (kset.has(tok)) cls = 'tok-key';
    else if (TYPE_RE.test(tok)) cls = 'tok-typ';
    else if (code[m.index + tok.length] === '(') cls = 'tok-fn';
    out += cls ? `<span class="${cls}">${esc(tok)}</span>` : esc(tok);
    last = m.index + tok.length;
  }
  out += esc(code.slice(last));
  return out;
}

/* ── WebSocket ───────────────────────────────────────────────────────── */
function setWsStatus(kind, label) {
  const node = $('statusWs');
  node.innerHTML = '';
  node.appendChild(el('span', 'dot ' + kind));
  node.appendChild(document.createTextNode(' ' + label));
}

function connect() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
  setWsStatus('busy', '连接中…');
  let sock;
  try { sock = new WebSocket(WS_URL); } catch { setTimeout(connect, 1500); return; }
  state.ws = sock;

  sock.onopen = () => {
    state.wsReady = true;
    state.wsRetries = 0;
    setWsStatus('online', '已连接');
  };
  sock.onclose = (ev) => {
    state.wsReady = false;
    state.streaming = false;
    if (ev && ev.code === 4401) {
      setWsStatus('offline', '已锁定');
      toast('服务端已锁定，请在设置中解锁', 'err');
      return;
    }
    setWsStatus('offline', '已断开');
    const delay = Math.min(1000 * Math.pow(1.6, state.wsRetries++), 15000);
    setTimeout(connect, delay);
  };
  sock.onerror = () => { /* onclose handles recovery */ };
  sock.onmessage = (ev) => {
    let frame;
    try { frame = JSON.parse(ev.data); } catch { return; }
    handleFrame(frame);
  };
}

function wsSend(obj) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    toast('连接未就绪，正在重连…', 'err');
    connect();
    return false;
  }
  state.ws.send(JSON.stringify(obj));
  return true;
}

setInterval(() => { if (state.wsReady) wsSend({ type: 'ping' }); }, 25000);

/* ── tool card helpers ───────────────────────────────────────────────── */
const TOOL_ICON = {
  shell_execute: '❯', file_read: '📄', file_write: '✎', file_edit: '✎',
  ls: '🗂', search_files: '🔍', web_fetch: '🌐', web_search: '🔍',
  browser_use: '🌐', memory_write: '🧠', memory_get: '🧠', skill_use: '⚡',
  subagent: '👥', send: '📨', read_image: '🖼', image_gen: '🎨',
};
const toolIcon = (n) => TOOL_ICON[n] || '⚙';

function toolSummary(name, input) {
  if (!input || typeof input !== 'object') return '';
  const pick = input.command || input.path || input.query || input.url
    || input.pattern || input.text || input.name || input.skill || '';
  const s = String(pick).replace(/\s+/g, ' ').trim();
  return s.length > 110 ? s.slice(0, 109) + '…' : s;
}

function makeToolCard(name, input) {
  const card = el('div', 'tool-card');
  card.dataset.toolName = name;
  const head = el('div', 'tool-head');
  head.appendChild(el('span', 'tool-chevron', '▶'));
  head.appendChild(el('span', 'tool-icon', toolIcon(name)));
  head.appendChild(el('span', 'tool-name', name));
  head.appendChild(el('span', 'tool-summary', toolSummary(name, input)));
  const status = el('span', 'tool-status running', '运行中…');
  head.appendChild(status);
  card.appendChild(head);

  const body = el('div', 'tool-body');
  if (input && Object.keys(input).length) {
    body.appendChild(el('div', 'tool-section-label', '参数'));
    const pre = el('pre', 'tool-pre', JSON.stringify(input, null, 2));
    body.appendChild(pre);
  }
  const outSec = el('div', 'tool-section');
  outSec.appendChild(el('div', 'tool-section-label', '输出'));
  const outPre = el('pre', 'tool-pre', '等待结果…');
  outSec.appendChild(outPre);
  outSec.style.display = 'none';
  body.appendChild(outSec);
  card.appendChild(body);

  head.addEventListener('click', () => card.classList.toggle('open'));
  card._status = status;
  card._outPre = outPre;
  card._outSec = outSec;
  card._head = head;
  return card;
}

function settleToolCard(card, ok, output, ms) {
  if (!card) return;
  card._status.className = 'tool-status ' + (ok ? 'ok' : 'err');
  card._status.textContent = (ok ? '✓' : '✗') + (ms != null ? ` ${ms}ms` : '');
  card._outSec.style.display = '';
  card._outPre.className = 'tool-pre' + (ok ? '' : ' err');
  card._outPre.textContent = output && output.length ? output : '(无输出)';
  if (!ok) card.classList.add('open');
}

/* ── streaming turn ──────────────────────────────────────────────────── */
function scrollChat(force) {
  const box = $('messages');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

function beginTurn() {
  emptyNode().style.display = 'none';
  const root = el('div', 'msg assistant');
  const role = el('div', 'msg-role');
  role.appendChild(el('span', 'avatar', '◈'));
  role.appendChild(document.createTextNode('OpenMinis'));
  root.appendChild(role);
  const body = el('div', 'msg-body');
  root.appendChild(body);
  $('messages').appendChild(root);
  state.turn = { root, body, textBlock: null, text: '', toolCards: new Map() };
  return state.turn;
}

function turnTextBlock() {
  const t = state.turn;
  if (!t) return null;
  if (!t.textBlock) {
    t.textBlock = el('div', 'stream-block');
    t.body.appendChild(t.textBlock);
  }
  return t.textBlock;
}

let deltaRaf = 0;
function appendDelta(text) {
  if (!state.turn) beginTurn();
  const t = state.turn;
  t.text += text;
  if (deltaRaf) return;
  deltaRaf = requestAnimationFrame(() => {
    deltaRaf = 0;
    const block = turnTextBlock();
    if (block) block.innerHTML = renderMarkdown(t.text) + '<span class="cursor-blink"></span>';
    scrollChat();
  });
}

function addToolCardToTurn(id, name, input) {
  const t = state.turn || beginTurn();
  // A tool call ends the current text run: the next delta starts a new block
  // *below* the card, which is what the agent's ordering actually means.
  t.textBlock = null;
  const card = makeToolCard(name, input);
  t.body.appendChild(card);
  if (id) t.toolCards.set(id, card);
  if (name === 'file_edit' || name === 'file_write') recordChange(name, input);
  scrollChat();
}

function endTurn() {
  const t = state.turn;
  if (!t) return;
  if (deltaRaf) { cancelAnimationFrame(deltaRaf); deltaRaf = 0; }
  const block = turnTextBlock();
  if (block) block.innerHTML = renderMarkdown(t.text);
  if (!t.text && !t.toolCards.size) {
    const body = el('div', 'msg-body');
    body.innerHTML = '<p style="color:var(--fg-faint)">(空回复)</p>';
    t.body.appendChild(body);
  }
  if (t.text) addMessageActions(t.root, t.text);
  state.turn = null;
  state.streaming = false;
  $('btnStop').hidden = true;
  setStatusTurn('');
  scrollChat(true);
  loadSessions();
}

function addMessageActions(root, text) {
  const bar = el('div', 'msg-actions');
  const copy = el('button', null, '复制');
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); toast('已复制'); }
    catch { toast('复制失败', 'err'); }
  });
  bar.appendChild(copy);
  root.appendChild(bar);
}

function setStatusTurn(txt) {
  $('statusTurn').textContent = txt;
  $('composerHint').textContent = txt;
}

/* ── frame dispatch ──────────────────────────────────────────────────── */
function handleFrame(f) {
  switch (f.type) {
    case 'delta':
      // Chat and shell deltas share a frame type; only chat carries a
      // sessionId. While a terminal command is in flight, route the
      // session-less ones to the drawer instead of the transcript.
      if (f.sessionId == null && state.termBusy) termAppend(f.text || '');
      else if (f.text) appendDelta(f.text);
      break;
    case 'toolStart':
      addToolCardToTurn(f.id, f.name, f.input);
      break;
    case 'toolEnd': {
      const t = state.turn;
      const card = t && f.id ? t.toolCards.get(f.id) : null;
      settleToolCard(card, f.ok, f.output, f.ms);
      if (f.images && f.images.length && t) {
        const wrap = el('div', 'tool-section-label', '生成图片');
        t.body.appendChild(wrap);
      }
      scrollChat();
      break;
    }
    case 'chatSession':
      if (f.sessionId && f.sessionId !== state.sessionId) {
        state.sessionId = f.sessionId;
        updateSessionHeader();
      }
      break;
    case 'usage':
      if (f.totalTokens != null) $('tokenUsage').textContent = `${f.totalTokens} tokens`;
      break;
    case 'done':
      if (f.exitCode != null && state.termBusy) {
        state.termBusy = false;
        termBuffer = null;
        if (f.exitCode !== 0) termLine(`[退出码 ${f.exitCode}]`, 'err');
      } else {
        endTurn();
      }
      break;
    case 'error':
      if (state.termBusy) {
        state.termBusy = false;
        termBuffer = null;
        termLine(f.error || '命令执行失败', 'err');
        break;
      }
      if (state.turn) { appendDelta(`\n\n> ⚠️ ${f.error}`); }
      else toast(f.error || '未知错误', 'err');
      endTurn();
      break;
    case 'pong':
      break;
    default:
      break;
  }
}

/* ── send ────────────────────────────────────────────────────────────── */
function send() {
  const box = $('input');
  const text = box.value.trim();
  if (!text) return;
  if (state.streaming) { toast('上一条还在处理中'); return; }

  emptyNode().style.display = 'none';
  const node = el('div', 'msg user');
  const role = el('div', 'msg-role');
  role.appendChild(el('span', 'avatar', '你'));
  role.appendChild(document.createTextNode('你'));
  node.appendChild(role);
  node.appendChild(el('div', 'msg-body', text));
  $('messages').appendChild(node);
  scrollChat(true);

  box.value = '';
  autoGrow();
  state.streaming = true;
  $('btnStop').hidden = false;
  setStatusTurn('思考中…');
  beginTurn();

  wsSend({ type: 'chat', text, session_id: state.sessionId || undefined });
}

function stopTurn() {
  wsSend({ type: 'stop' });
  setStatusTurn('已请求停止…');
}

/* ── sessions ────────────────────────────────────────────────────────── */
async function loadSessions() {
  try {
    const data = await api('/chats/sessions');
    state.sessions = (data && data.sessions) || [];
    renderSessions();
  } catch (e) {
    toast('会话列表加载失败: ' + e.message, 'err');
  }
}

function renderSessions() {
  const list = $('sessionList');
  list.innerHTML = '';
  const q = state.filter.toLowerCase();
  const rows = state.sessions.filter((s) => !q || String(s.title || '').toLowerCase().includes(q));
  if (!rows.length) {
    list.appendChild(el('div', 'tree-empty', state.sessions.length ? '没有匹配的会话' : '还没有会话'));
    return;
  }
  for (const s of rows) {
    const item = el('div', 'session-item' + (s.id === state.sessionId ? ' active' : ''));
    item.appendChild(el('div', 's-title', s.title || '(未命名)'));
    item.appendChild(el('div', 's-time', relTime(s.updatedAt || s.createdAt)));
    const del = el('button', 's-del', '✕');
    del.title = '删除会话';
    del.addEventListener('click', (ev) => { ev.stopPropagation(); deleteSession(s.id); });
    item.appendChild(del);
    item.addEventListener('click', () => selectSession(s.id));
    list.appendChild(item);
  }
}

function updateSessionHeader() {
  const s = state.sessions.find((x) => x.id === state.sessionId);
  $('chatTitle').textContent = s ? (s.title || '(未命名会话)') : (state.sessionId ? '会话' : '未选择会话');
  $('statusSession').textContent = state.sessionId ? shortPath(state.sessionId, 18) : '—';
  renderSessions();
}

async function newSession() {
  try {
    const s = await api('/chats/sessions', { method: 'POST', body: JSON.stringify({}) });
    const id = s && (s.id || (s.session && s.session.id));
    if (id) {
      state.sessionId = id;
      state.messages = [];
      state.changes = [];
      $('messages').innerHTML = '';
      $('messages').appendChild(emptyNode());
      emptyNode().style.display = '';
      renderDiff();
      await loadSessions();
      updateSessionHeader();
      $('input').focus();
      toast('已创建新会话');
    }
  } catch (e) { toast('创建会话失败: ' + e.message, 'err'); }
}

async function selectSession(id) {
  if (id === state.sessionId) return;
  state.sessionId = id;
  state.changes = [];
  updateSessionHeader();
  $('messages').innerHTML = '';
  $('messages').appendChild(emptyNode());
  emptyNode().style.display = 'none';
  try {
    const data = await api(`/chats/sessions/${encodeURIComponent(id)}/messages`);
    renderMessages((data && data.messages) || []);
  } catch (e) {
    toast('消息加载失败: ' + e.message, 'err');
  }
  $('input').focus();
}

async function deleteSession(id) {
  if (!confirm('删除这个会话？')) return;
  try {
    await api(`/chats/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (state.sessionId === id) {
      state.sessionId = null;
      state.messages = [];
      $('messages').innerHTML = '';
      $('messages').appendChild(emptyNode());
      emptyNode().style.display = '';
      updateSessionHeader();
    }
    await loadSessions();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

/* ── history rendering ───────────────────────────────────────────────── */
function renderMessages(messages) {
  const box = $('messages');
  box.innerHTML = '';
  if (!messages.length) {
    box.appendChild(emptyNode());
    emptyNode().style.display = '';
    return;
  }
  state.messages = messages;
  for (const m of messages) {
    const isUser = m.role === 'user';
    const node = el('div', 'msg ' + (isUser ? 'user' : 'assistant'));
    const role = el('div', 'msg-role');
    role.appendChild(el('span', 'avatar', isUser ? '你' : '◈'));
    role.appendChild(document.createTextNode(isUser ? '你' : 'OpenMinis'));
    node.appendChild(role);

    const body = el('div', 'msg-body');
    if (isUser) body.textContent = m.text || '';
    else body.innerHTML = renderMarkdown(m.text || '');
    node.appendChild(body);

    const runs = Array.isArray(m.runs) ? m.runs : [];
    if (runs.length) {
      const wrap = el('div', 'turn-tools');
      for (const r of runs) {
        const card = makeToolCard(r.name || 'tool', r.input || {});
        settleToolCard(card, r.ok !== false, r.output || '', r.ms);
        wrap.appendChild(card);
        if (r.name === 'file_edit' || r.name === 'file_write') recordChange(r.name, r.input || {});
      }
      node.appendChild(wrap);
    }
    if (!isUser && m.text) addMessageActions(node, m.text);
    box.appendChild(node);
  }
  renderDiff();
  scrollChat(true);
}

/* ── file changes / diff ─────────────────────────────────────────────── */
function recordChange(kind, input) {
  if (!input || typeof input !== 'object') return;
  const path = input.path || input.file_path || input.filePath || '';
  if (!path) return;
  state.changes.push({
    kind,
    path: String(path),
    old: kind === 'file_edit' ? (input.old_string ?? input.oldString ?? '') : '',
    neu: kind === 'file_edit' ? (input.new_string ?? input.newString ?? '') : (input.content ?? ''),
  });
  renderDiff();
}

function diffRows(oldText, newText) {
  // Line diff by longest-common-subsequence. The snippets an agent edits are
  // small, so the O(n*m) table is affordable and the result reads far better
  // than a naive "all deletions then all insertions" listing.
  const a = String(oldText).replace(/\n$/, '').split('\n');
  const b = String(newText).replace(/\n$/, '').split('\n');
  if (a.length > 1500 || b.length > 1500) {
    return a.map((l) => ({ t: 'del', l })).concat(b.map((l) => ({ t: 'add', l })));
  }
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: 'ctx', l: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: 'del', l: a[i] }); i++; }
    else { out.push({ t: 'add', l: b[j] }); j++; }
  }
  while (i < n) out.push({ t: 'del', l: a[i++] });
  while (j < m) out.push({ t: 'add', l: b[j++] });
  return out;
}

function renderDiff() {
  const view = $('diffView');
  view.innerHTML = '';
  if (!state.changes.length) {
    view.appendChild(el('div', 'placeholder', 'Agent 编辑文件后，这里显示 diff'));
    return;
  }
  for (const ch of state.changes.slice(-12).reverse()) {
    const head = el('div', 'panel-head');
    head.appendChild(el('span', 'panel-title', ch.path));
    head.appendChild(el('span', 'meta-item', ch.kind === 'file_edit' ? '编辑' : '写入'));
    view.appendChild(head);

    const rows = ch.kind === 'file_edit' ? diffRows(ch.old, ch.neu) : diffRows('', ch.neu);
    const table = el('table', 'diff-table');
    const tbody = el('tbody');
    let ln = 0;
    for (const r of rows) {
      if (r.t !== 'del') ln++;
      const tr = el('tr', r.t);
      tr.appendChild(el('td', 'ln', r.t === 'del' ? '' : String(ln)));
      const sign = r.t === 'add' ? '+ ' : r.t === 'del' ? '- ' : '  ';
      const td = el('td', null);
      td.innerHTML = esc(sign + r.l);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    view.appendChild(table);
  }
}

/* ── file tree ───────────────────────────────────────────────────────── */
const FILE_ICON = (name, isDir) => {
  if (isDir) return '📁';
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['py'].includes(ext)) return '🐍';
  if (['js', 'mjs', 'cjs'].includes(ext)) return '🟨';
  if (['ts', 'tsx', 'jsx'].includes(ext)) return '🔷';
  if (['json', 'yaml', 'yml', 'toml', 'ini', 'conf'].includes(ext)) return '⚙';
  if (['md', 'txt', 'rst'].includes(ext)) return '📝';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico'].includes(ext)) return '🖼';
  if (['sh', 'bash', 'zsh', 'bat', 'ps1'].includes(ext)) return '❯';
  if (['html', 'htm', 'css', 'scss'].includes(ext)) return '🎨';
  return '📄';
};

async function loadTree(path = '', container = $('fileTree'), depth = 2) {
  container.innerHTML = '<div class="tree-empty">加载中…</div>';
  try {
    const data = await api(`/fs/tree?path=${encodeURIComponent(path)}&depth=${depth}`);
    container.innerHTML = '';
    const kids = (data && data.children) || [];
    if (!kids.length) {
      container.appendChild(el('div', 'tree-empty', '工作目录为空'));
      return;
    }
    container.appendChild(buildTreeNodes(kids, container));
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(el('div', 'tree-empty', '无法读取工作目录: ' + e.message));
  }
}

function buildTreeNodes(nodes, container) {
  const frag = document.createDocumentFragment();
  for (const n of nodes) {
    const row = el('div', 'tree-row');
    row.dataset.path = n.path;
    row.appendChild(el('span', 't-icon', FILE_ICON(n.name, n.isDir)));
    row.appendChild(el('span', 't-name', n.name));
    if (!n.isDir && n.size != null) row.appendChild(el('span', 't-size', fmtBytes(n.size)));
    frag.appendChild(row);

    if (n.isDir) {
      const kids = el('div', 'tree-children');
      kids.hidden = true;
      frag.appendChild(kids);
      row.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        if (kids.hidden && !kids.dataset.loaded) {
          kids.dataset.loaded = '1';
          const sub = document.createDocumentFragment();
          sub.appendChild(el('div', 'tree-empty', '加载中…'));
          kids.appendChild(sub);
          try {
            const data = await api(`/fs/tree?path=${encodeURIComponent(n.path)}&depth=1`);
            kids.innerHTML = '';
            const inner = (data && data.children) || [];
            kids.appendChild(inner.length ? buildTreeNodes(inner, container) : el('div', 'tree-empty', '(空)'));
          } catch (e) {
            kids.innerHTML = '';
            kids.appendChild(el('div', 'tree-empty', '读取失败'));
          }
        }
        kids.hidden = !kids.hidden;
      });
    } else {
      row.addEventListener('click', () => openFile(n.path, container));
    }
  }
  return frag;
}

/* ── code viewer ─────────────────────────────────────────────────────── */
function langOf(path) {
  const ext = (String(path).split('.').pop() || '').toLowerCase();
  return ({ py: 'py', js: 'js', mjs: 'js', cjs: 'js', ts: 'ts', tsx: 'ts', jsx: 'js',
    sh: 'sh', bash: 'sh', zsh: 'sh', go: 'go', rs: 'rust', c: 'c', h: 'c', cpp: 'c',
    java: 'js', kt: 'js', swift: 'js', json: 'js', yaml: 'yaml', yml: 'yaml',
    toml: 'toml', ini: 'ini', conf: 'ini' })[ext] || 'js';
}

async function openFile(path, container) {
  switchTab('code');
  $('codePath').textContent = shortPath(path, 60);
  const view = $('codeView');
  view.innerHTML = '<div class="placeholder">读取中…</div>';
  try {
    const data = await api(`/fs/read?path=${encodeURIComponent(path)}&maxBytes=200000`);
    const content = (data && (data.text ?? data.content)) || '';
    state.currentFile = path;
    if (container) {
      container.querySelectorAll('.tree-row.active').forEach((r) => r.classList.remove('active'));
      const row = container.querySelector(`.tree-row[data-path="${CSS.escape(path)}"]`);
      if (row) row.classList.add('active');
    }
    view.innerHTML = '';
    view.appendChild(renderCode(content, langOf(path)));
    if (data && data.truncated) {
      view.appendChild(el('div', 'placeholder', '（文件较大，已截断显示）'));
    }
  } catch (e) {
    view.innerHTML = '';
    view.appendChild(el('div', 'placeholder', '读取失败: ' + e.message));
  }
}

function renderCode(content, lang) {
  const lines = String(content).replace(/\n$/, '').split('\n');
  const table = el('table', 'code-table');
  const tbody = el('tbody');
  const MAX_HL = 3000;
  const highlighted = lines.length <= MAX_HL ? highlight(String(content).replace(/\n$/, ''), lang).split('\n') : null;
  for (let i = 0; i < lines.length; i++) {
    const tr = el('tr');
    tr.appendChild(el('td', 'ln', String(i + 1)));
    const td = el('td', 'src');
    if (highlighted && highlighted[i] != null) td.innerHTML = highlighted[i] || '&nbsp;';
    else td.textContent = lines[i];
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

/* ── terminal ────────────────────────────────────────────────────────── */
function termLine(text, cls) {
  const body = $('termBody');
  const node = el('div', 'term-line' + (cls ? ' ' + cls : ''), text);
  body.appendChild(node);
  body.scrollTop = body.scrollHeight;
  return node;
}

function toggleTerminal(force) {
  const term = $('terminal');
  const show = force != null ? force : term.hidden;
  term.hidden = !show;
  $('btnTerm').classList.toggle('on', show);
  if (show) $('termInput').focus();
}

let termBuffer = null;
async function runShell(cmd) {
  if (!cmd.trim()) return;
  termLine('$ ' + cmd, 'cmd');
  state.termHistory.push(cmd);
  state.termIdx = state.termHistory.length;
  if (state.termBusy) { toast('上一条命令还在执行'); return; }
  state.termBusy = true;
  termBuffer = { node: null, text: '' };
  const ok = wsSend({ type: 'shell', command: cmd });
  if (!ok) { state.termBusy = false; termLine('连接不可用', 'err'); }
}

function termAppend(text) {
  if (!termBuffer) { termLine(text); return; }
  termBuffer.text += text;
  if (!termBuffer.node) termBuffer.node = termLine('', null);
  termBuffer.node.textContent = termBuffer.text;
  $('termBody').scrollTop = $('termBody').scrollHeight;
}

/* ── inspector tabs ──────────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
  $('inspector').classList.remove('hidden');
  $('splitInspector').classList.remove('hidden');
  $('btnInspector').classList.add('on');
  try { localStorage.setItem('om.tab', name); } catch { /* private mode */ }
}

function toggleInspector(force) {
  const hidden = $('inspector').classList.contains('hidden');
  const show = force != null ? force : hidden;
  $('inspector').classList.toggle('hidden', !show);
  $('splitInspector').classList.toggle('hidden', !show);
  $('btnInspector').classList.toggle('on', show);
  try { localStorage.setItem('om.inspector', show ? '1' : '0'); } catch { /* ignore */ }
}

/* ── resizable panes ─────────────────────────────────────────────────── */
function initSplitters() {
  const pairs = [
    { splitter: 'splitRail', pane: 'rail', varName: '--rail-w', min: 190, max: 460, invert: false },
    { splitter: 'splitInspector', pane: 'inspector', varName: '--inspector-w', min: 250, max: 760, invert: true },
  ];
  for (const cfg of pairs) {
    const node = $(cfg.splitter);
    if (!node) continue;
    const saved = (() => { try { return localStorage.getItem('om.' + cfg.varName); } catch { return null; } })();
    if (saved) document.documentElement.style.setProperty(cfg.varName, saved + 'px');

    node.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      node.classList.add('dragging');
      node.setPointerCapture(ev.pointerId);
      const startX = ev.clientX;
      const startW = $(cfg.pane).getBoundingClientRect().width;
      const move = (e) => {
        const delta = cfg.invert ? startX - e.clientX : e.clientX - startX;
        const w = Math.max(cfg.min, Math.min(cfg.max, startW + delta));
        document.documentElement.style.setProperty(cfg.varName, w + 'px');
      };
      const up = (e) => {
        node.classList.remove('dragging');
        node.removeEventListener('pointermove', move);
        node.removeEventListener('pointerup', up);
        try {
          const w = $(cfg.pane).getBoundingClientRect().width;
          localStorage.setItem('om.' + cfg.varName, String(Math.round(w)));
        } catch { /* ignore */ }
        e.preventDefault();
      };
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerup', up);
    });
  }
}

/* ── command palette ─────────────────────────────────────────────────── */
function paletteCommands() {
  const cmds = [
    { kind: 'action', label: '新建会话', run: () => newSession() },
    { kind: 'action', label: '切换终端面板', run: () => toggleTerminal() },
    { kind: 'action', label: '切换右侧面板', run: () => toggleInspector() },
    { kind: 'action', label: '刷新文件树', run: () => loadTree() },
    { kind: 'action', label: '刷新会话列表', run: () => loadSessions() },
    { kind: 'action', label: '切换主题（深色 / 浅色）', run: () => toggleTheme() },
    { kind: 'action', label: '查看技能列表', run: () => showSkills() },
    { kind: 'action', label: '查看记忆文件', run: () => showMemory() },
    { kind: 'action', label: '运行信息 / 诊断', run: () => showInfo() },
    { kind: 'action', label: '打开工作目录', run: () => openWorkspace() },
    { kind: 'tab', label: '面板：文件', run: () => switchTab('files') },
    { kind: 'tab', label: '面板：代码', run: () => switchTab('code') },
    { kind: 'tab', label: '面板：变更', run: () => switchTab('diff') },
    { kind: 'tab', label: '面板：信息', run: () => switchTab('info') },
  ];
  for (const s of state.sessions.slice(0, 60)) {
    cmds.push({ kind: 'session', label: '会话：' + (s.title || '(未命名)'), run: () => selectSession(s.id) });
  }
  return cmds;
}

function openPalette() {
  state.palette.open = true;
  state.palette.sel = 0;
  $('paletteOverlay').hidden = false;
  const inp = $('paletteInput');
  inp.value = '';
  renderPalette('');
  inp.focus();
}

function closePalette() {
  state.palette.open = false;
  $('paletteOverlay').hidden = true;
}

function renderPalette(q) {
  const all = paletteCommands();
  const query = q.trim().toLowerCase();
  const items = query ? all.filter((c) => c.label.toLowerCase().includes(query)) : all;
  state.palette.items = items;
  if (state.palette.sel >= items.length) state.palette.sel = Math.max(0, items.length - 1);
  const list = $('paletteList');
  list.innerHTML = '';
  if (!items.length) {
    list.appendChild(el('div', 'palette-empty', '没有匹配项'));
    return;
  }
  items.forEach((c, i) => {
    const row = el('div', 'palette-item' + (i === state.palette.sel ? ' sel' : ''));
    row.appendChild(el('span', 'p-label', c.label));
    row.appendChild(el('span', 'p-kind', c.kind));
    row.addEventListener('click', () => { closePalette(); c.run(); });
    list.appendChild(row);
  });
}

function paletteKey(ev) {
  const items = state.palette.items;
  if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    state.palette.sel = Math.min(items.length - 1, state.palette.sel + 1);
    renderPalette($('paletteInput').value);
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    state.palette.sel = Math.max(0, state.palette.sel - 1);
    renderPalette($('paletteInput').value);
  } else if (ev.key === 'Enter') {
    ev.preventDefault();
    const c = items[state.palette.sel];
    closePalette();
    if (c) c.run();
  } else if (ev.key === 'Escape') {
    closePalette();
  }
}

/* ── modal panels ────────────────────────────────────────────────────── */
function openModal(title, node) {
  $('modalTitle').textContent = title;
  const body = $('modalBody');
  body.innerHTML = '';
  body.appendChild(node);
  $('modalOverlay').hidden = false;
}

async function showSkills() {
  openModal('技能', el('div', 'placeholder', '加载中…'));
  try {
    const data = await api('/skills');
    const list = (data && (data.skills || data.items)) || [];
    const wrap = el('div', null);
    if (!list.length) {
      wrap.appendChild(el('div', 'placeholder', '还没有安装技能'));
    } else {
      for (const s of list) {
        const row = el('div', 'info-row');
        row.appendChild(el('span', 'info-k', s.name || s.id || '?'));
        row.appendChild(el('span', 'info-v', s.description || s.summary || ''));
        wrap.appendChild(row);
      }
    }
    openModal(`技能（${list.length}）`, wrap);
  } catch (e) {
    openModal('技能', el('div', 'placeholder', '加载失败: ' + e.message));
  }
}

async function showMemory() {
  openModal('记忆', el('div', 'placeholder', '加载中…'));
  try {
    const data = await api('/system/memory');
    const files = (data && (data.files || data.entries)) || [];
    const wrap = el('div', null);
    if (!files.length) wrap.appendChild(el('div', 'placeholder', '记忆为空'));
    for (const f of files) {
      const row = el('div', 'info-row');
      row.appendChild(el('span', 'info-k', f.name || f.path || '?'));
      row.appendChild(el('span', 'info-v', fmtBytes(f.size) + '  ' + (f.modified || f.updatedAt || '')));
      wrap.appendChild(row);
    }
    openModal(`记忆（${files.length}）`, wrap);
  } catch (e) {
    openModal('记忆', el('div', 'placeholder', '加载失败: ' + e.message));
  }
}

async function loadInfo() {
  const rows = [];
  try {
    state.info = await api('/desktop/info');
    for (const [k, v] of Object.entries(state.info)) {
      rows.push([k, v == null ? '—' : String(v)]);
    }
  } catch (e) { rows.push(['desktop/info', '读取失败: ' + e.message]); }
  try {
    const h = await api('/health');
    for (const [k, v] of Object.entries(h)) rows.push(['health.' + k, String(v)]);
  } catch { /* offline is fine */ }
  renderInfoTab(rows);
  return rows;
}

async function showInfo() {
  openModal('运行信息', el('div', 'placeholder', '加载中…'));
  const rows = await loadInfo();
  const wrap = el('div', null);
  for (const [k, v] of rows) {
    const r = el('div', 'info-row');
    r.appendChild(el('span', 'info-k', k));
    r.appendChild(el('span', 'info-v', v));
    wrap.appendChild(r);
  }
  openModal('运行信息', wrap);
}

function renderInfoTab(rows) {
  const list = $('infoList');
  list.innerHTML = '';
  for (const [k, v] of rows) {
    const r = el('div', 'info-row');
    r.appendChild(el('span', 'info-k', k));
    r.appendChild(el('span', 'info-v', v));
    list.appendChild(r);
  }
}

function openWorkspace() {
  const p = state.info && state.info.workspace;
  if (!p) { toast('工作目录未知'); return; }
  if (window.pywebview && window.pywebview.api && window.pywebview.api.open_in_file_manager) {
    window.pywebview.api.open_in_file_manager(p);
  } else {
    navigator.clipboard.writeText(p).then(() => toast('路径已复制: ' + p)).catch(() => toast(p));
  }
}

/* ── theme ───────────────────────────────────────────────────────────── */
function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  try { localStorage.setItem('om.theme', name); } catch { /* ignore */ }
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
}

/* ── composer autogrow ───────────────────────────────────────────────── */
function autoGrow() {
  const t = $('input');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 210) + 'px';
}

/* ── keyboard ────────────────────────────────────────────────────────── */
function onKeydown(ev) {
  const mod = ev.ctrlKey || ev.metaKey;
  const inField = ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName);

  if (mod && ev.key.toLowerCase() === 'k') { ev.preventDefault(); openPalette(); return; }
  if (mod && ev.key === '`') { ev.preventDefault(); toggleTerminal(); return; }
  if (mod && ev.key.toLowerCase() === 'b') { ev.preventDefault(); toggleInspector(); return; }
  if (mod && ev.key.toLowerCase() === 'n') { ev.preventDefault(); newSession(); return; }
  if (ev.key === 'Escape') {
    if (state.palette.open) { closePalette(); return; }
    if (!$('modalOverlay').hidden) { $('modalOverlay').hidden = true; return; }
    if (state.streaming) { stopTurn(); return; }
  }
  if (ev.key === 'Enter' && !ev.shiftKey && document.activeElement === $('input')) {
    ev.preventDefault();
    send();
    return;
  }
  if (ev.key === 'Enter' && document.activeElement === $('termInput')) {
    ev.preventDefault();
    const v = $('termInput').value;
    $('termInput').value = '';
    runShell(v);
    return;
  }
  // shell-style history in the terminal
  if (document.activeElement === $('termInput')) {
    if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (state.termIdx > 0) { state.termIdx--; $('termInput').value = state.termHistory[state.termIdx] || ''; }
    } else if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      if (state.termIdx < state.termHistory.length - 1) {
        state.termIdx++;
        $('termInput').value = state.termHistory[state.termIdx] || '';
      } else { state.termIdx = state.termHistory.length; $('termInput').value = ''; }
    }
  }
  if (ev.key === 'Tab' && !inField) ev.preventDefault();
}

/* ── wiring ──────────────────────────────────────────────────────────── */
function wire() {
  $('btnNewSession').addEventListener('click', newSession);
  $('btnReloadSessions').addEventListener('click', loadSessions);
  $('sessionFilter').addEventListener('input', (e) => { state.filter = e.target.value; renderSessions(); });

  $('btnSend').addEventListener('click', send);
  $('btnStop').addEventListener('click', stopTurn);
  $('input').addEventListener('input', autoGrow);
  $('input').addEventListener('keydown', onKeydown);

  $('btnPalette').addEventListener('click', openPalette);
  $('paletteOverlay').addEventListener('click', (e) => { if (e.target === $('paletteOverlay')) closePalette(); });
  $('paletteInput').addEventListener('input', (e) => { state.palette.sel = 0; renderPalette(e.target.value); });
  $('paletteInput').addEventListener('keydown', paletteKey);

  $('btnTerm').addEventListener('click', () => toggleTerminal());
  $('btnHideTerm').addEventListener('click', () => toggleTerminal(false));
  $('btnClearTerm').addEventListener('click', () => { $('termBody').innerHTML = ''; });
  $('termInput').addEventListener('keydown', onKeydown);

  $('btnInspector').addEventListener('click', () => toggleInspector());
  $('btnTheme').addEventListener('click', toggleTheme);
  $('btnInfo').addEventListener('click', showInfo);
  $('btnSkills').addEventListener('click', showSkills);
  $('btnMemory').addEventListener('click', showMemory);
  $('btnRefreshTree').addEventListener('click', () => loadTree());
  $('btnCopyCode').addEventListener('click', async () => {
    if (!state.currentFile) return;
    try {
      const data = await api(`/fs/read?path=${encodeURIComponent(state.currentFile)}&maxBytes=200000`);
      await navigator.clipboard.writeText((data && (data.text ?? data.content)) || '');
      toast('已复制文件内容');
    } catch (e) { toast('复制失败: ' + e.message, 'err'); }
  });
  $('btnCloseCode').addEventListener('click', () => {
    state.currentFile = null;
    $('codePath').textContent = '未打开文件';
    $('codeView').innerHTML = '<div class="placeholder">在左侧文件树中选择一个文件</div>';
  });

  $('modalClose').addEventListener('click', () => { $('modalOverlay').hidden = true; });
  $('modalOverlay').addEventListener('click', (e) => { if (e.target === $('modalOverlay')) $('modalOverlay').hidden = true; });

  $('wsChip').addEventListener('click', openWorkspace);
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  document.addEventListener('keydown', onKeydown);
}

/* ── boot ────────────────────────────────────────────────────────────── */
async function boot() {
  try { applyTheme(localStorage.getItem('om.theme') || 'dark'); } catch { applyTheme('dark'); }
  try { if (localStorage.getItem('om.inspector') === '0') toggleInspector(false); } catch { /* ignore */ }
  try { switchTab(localStorage.getItem('om.tab') || 'files'); } catch { switchTab('files'); }

  wire();
  initSplitters();
  connect();
  termLine('OpenMinis Desktop — 终端已就绪（命令在本机 shell 中执行）', 'sys');

  await loadSessions();
  loadTree();
  loadInfo();
  $('input').focus();
  autoGrow();
}

document.addEventListener('DOMContentLoaded', boot);
