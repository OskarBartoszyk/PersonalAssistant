/* Chat + two-way voice.

   Voice uses the browser's own Web Speech API rather than a server round
   trip: recognition and synthesis both run locally, so listening and
   speaking are instant and cost nothing. The only latency in a spoken
   exchange is the model itself.

   The reply channel follows how the message was sent, which is what makes
   the interaction feel natural:
     - spoken  -> the assistant speaks its answer aloud (and shows it)
     - typed   -> the assistant answers in the chat panel only

   Status frames from /api/chat/stream are rendered in the typing bubble so
   the ~10-15s of local generation shows real progress. */

import { state, api, nowHHMM, escapeHtml, toast } from './api.js';
import { reloadEvents } from './calendar.js';
import { reloadReminders, reloadNotes } from './panels.js';

const el = (id) => document.getElementById(id);

let chatOpen = true;
let busy = false;

/* ====================== panel ====================== */

export function setChatOpen(open) {
  chatOpen = open;
  el('appShell').classList.toggle('chat-collapsed', !open);
  el('chatTab').querySelector('.chevron').textContent = open ? '›' : '‹';
}

export function renderChat() {
  const log = el('chatLog');
  log.innerHTML = '';
  if (!state.chat.length) {
    log.appendChild(el('chatLogEmpty'));
    return;
  }
  state.chat.forEach(m => {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + m.role + (m.typing ? ' typing' : '');
    wrap.innerHTML =
      `<div class="msg-bubble">${escapeHtml(m.text)}</div>` +
      `<div class="msg-time">${escapeHtml(m.time || '')}</div>`;
    log.appendChild(wrap);
  });
  log.scrollTop = log.scrollHeight;
}

function push(role, text, opts = {}) {
  const m = { role, text, time: opts.time ?? nowHHMM(), typing: !!opts.typing };
  state.chat.push(m);
  renderChat();
  return m;
}

function replace(msg, text, typing) {
  msg.text = text;
  msg.typing = !!typing;
  renderChat();
}

/* ====================== sending ====================== */

/** Reads an SSE body with fetch, since EventSource cannot POST. */
async function streamChat(message, onFrame) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message })
  });
  if (!res.ok || !res.body) throw new Error('stream failed');

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const chunk = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (!chunk.startsWith('data:')) continue;
      try { onFrame(JSON.parse(chunk.slice(5).trim())); }
      catch { /* ignore malformed frame */ }
    }
  }
}

export async function sendMessage(text, { spoken = false } = {}) {
  const message = (text || '').trim();
  if (!message) return;
  if (busy) {
    // Dropping input in silence is what makes the app feel broken.
    push('assistant', 'Chwila — jeszcze odpowiadam na poprzednie pytanie.');
    return;
  }
  busy = true;

  push('user', message);
  if (!chatOpen) setChatOpen(true);
  const typing = push('assistant', 'Myślę…', { typing: true, time: '' });

  let finalText = '';
  try {
    await streamChat(message, (frame) => {
      if (frame.type === 'status') replace(typing, frame.text, true);
      else if (frame.type === 'final' || frame.type === 'error') {
        finalText = frame.text || '';
        replace(typing, finalText, false);
        typing.time = nowHHMM();
        if (frame.changed) refreshAll();
        renderChat();
      }
    });
  } catch {
    replace(typing, 'Nie mogę połączyć się z backendem. Uruchom: uvicorn main:app --reload --port 8000', false);
    busy = false;
    return;
  }

  busy = false;
  if (spoken && finalText) speak(finalText);
}

async function refreshAll() {
  await Promise.all([reloadEvents(), reloadReminders(), reloadNotes()]);
}

/* ====================== speech synthesis ====================== */

let voice = null;
let ttsPrimed = false;

/** Chrome blocks speechSynthesis.speak() when it is not close enough to a
    user gesture. Our reply arrives 10-20s after the click that started
    listening, which is well past that window, so the answer would be
    computed and then never spoken. Speaking a silent utterance during the
    click itself unlocks synthesis for the rest of the session. */
export function primeSpeech() {
  if (ttsPrimed || !('speechSynthesis' in window)) return;
  try {
    const u = new SpeechSynthesisUtterance(' ');
    u.volume = 0;
    window.speechSynthesis.speak(u);
    ttsPrimed = true;
  } catch { /* not fatal — speech may still work */ }
}

function pickVoice() {
  const voices = window.speechSynthesis?.getVoices?.() || [];
  if (!voices.length) return null;
  return voices.find(v => v.lang === 'pl-PL')
      || voices.find(v => v.lang?.startsWith('pl'))
      || voices.find(v => v.default)
      || voices[0];
}

if ('speechSynthesis' in window) {
  // Voice list populates asynchronously in most browsers.
  window.speechSynthesis.addEventListener('voiceschanged', () => { voice = pickVoice(); });
  voice = pickVoice();
}

export function speak(text) {
  if (!('speechSynthesis' in window)) {
    console.warn('[voice] speechSynthesis unavailable');
    return;
  }
  if (!text) return;
  stopSpeaking();

  const u = new SpeechSynthesisUtterance(text);
  voice = voice || pickVoice();
  if (voice) u.voice = voice;
  u.lang = voice?.lang || 'pl-PL';
  u.rate = 1.04;
  u.pitch = 1.0;

  const btn = el('voiceBtn');
  u.onstart = () => btn.classList.add('speaking');
  u.onend = () => btn.classList.remove('speaking');
  u.onerror = (e) => {
    btn.classList.remove('speaking');
    // 'interrupted'/'canceled' are normal when we stop it ourselves.
    if (e.error && e.error !== 'interrupted' && e.error !== 'canceled') {
      console.warn('[voice] synthesis error:', e.error);
      toast('Nie mogę odczytać odpowiedzi: ' + e.error);
    }
  };

  window.speechSynthesis.speak(u);
}

export function stopSpeaking() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  el('voiceBtn')?.classList.remove('speaking');
}

/* ====================== speech recognition ====================== */

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let listening = false;
let micGranted = false;

/** Voice problems must never fail silently — an unexplained dead mic is
    indistinguishable from a broken app. Everything lands in the chat log,
    which is persistent, rather than a toast that can be missed. */
function voiceError(text) {
  push('assistant', text);
  console.warn('[voice]', text);
}

const ERRORS = {
  'not-allowed': 'Brak dostępu do mikrofonu. Kliknij kłódkę obok adresu w pasku przeglądarki i zezwól na mikrofon, potem spróbuj ponownie.',
  'service-not-allowed': 'Przeglądarka zablokowała rozpoznawanie mowy. W Brave/Firefox to nie zadziała — użyj Chrome.',
  'audio-capture': 'Nie znaleziono mikrofonu. Sprawdź, czy jest podłączony i wybrany w ustawieniach systemu.',
  'network': 'Rozpoznawanie mowy w Chrome wymaga internetu (audio idzie na serwery Google). Sprawdź połączenie.',
  'aborted': null,      // user-initiated stop, not an error
  'no-speech': 'Nie usłyszałem nic. Kliknij mikrofon i mów wyraźnie.'
};

/** Ask for the mic explicitly. SpeechRecognition's own permission flow is
    opaque — if it is blocked it often just ends with no event at all, which
    is exactly the "nothing happens" symptom. getUserMedia gives a real error. */
async function ensureMic() {
  if (micGranted) return true;
  if (!navigator.mediaDevices?.getUserMedia) {
    voiceError('Ta przeglądarka nie daje dostępu do mikrofonu. Użyj Chrome przez http://localhost:8000.');
    return false;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach(t => t.stop());   // we only needed the permission
    micGranted = true;
    return true;
  } catch (err) {
    voiceError(ERRORS['not-allowed'] + ` (${err.name})`);
    return false;
  }
}

function buildRecognition() {
  const r = new SR();
  r.lang = 'pl-PL';
  r.continuous = false;      // one utterance per press — clearer turn-taking
  r.interimResults = true;
  r.maxAlternatives = 1;

  let finalText = '';
  let sawError = false;

  r.onstart = () => {
    listening = true;
    finalText = '';
    sawError = false;
    el('voiceBtn').classList.add('listening');
    el('voiceTranscript').hidden = false;
    el('vtFinal').textContent = '';
    el('vtInterim').textContent = '';
  };

  r.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const chunk = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += chunk;
      else interim += chunk;
    }
    el('vtFinal').textContent = finalText;
    el('vtInterim').textContent = interim;
  };

  r.onerror = (e) => {
    sawError = true;
    listening = false;
    el('voiceBtn').classList.remove('listening');
    el('voiceTranscript').hidden = true;
    const msg = e.error in ERRORS ? ERRORS[e.error] : `Błąd mikrofonu: ${e.error}`;
    if (msg) voiceError(msg);
  };

  r.onend = () => {
    listening = false;
    el('voiceBtn').classList.remove('listening');
    el('voiceTranscript').hidden = true;
    const said = finalText.trim();
    if (said) {
      sendMessage(said, { spoken: true });          // spoken in -> spoken out
    } else if (!sawError) {
      voiceError('Nie usłyszałem nic. Kliknij mikrofon i mów wyraźnie.');
    }
  };

  return r;
}

export async function toggleListening() {
  if (!SR) {
    voiceError('Ta przeglądarka nie obsługuje rozpoznawania mowy. Otwórz aplikację w Chrome — w Firefox, Brave i Arc to API jest wyłączone.');
    return;
  }
  // Unlock synthesis now, while we still have the click. By the time the
  // reply exists, Chrome would refuse to speak it.
  primeSpeech();

  // Never listen while talking, or the assistant hears itself.
  stopSpeaking();

  if (listening) { recognition?.stop(); return; }

  // Speaking while the model is still working used to be dropped in silence.
  if (busy) {
    voiceError('Chwila — jeszcze odpowiadam na poprzednie pytanie. Spróbuj za moment.');
    return;
  }

  if (!(await ensureMic())) return;

  recognition = recognition || buildRecognition();
  try {
    recognition.start();
  } catch (err) {
    // start() throws if a previous session is still winding down.
    if (err.name === 'InvalidStateError') {
      try { recognition.abort(); } catch { /* ignore */ }
      setTimeout(() => { try { recognition.start(); } catch { /* ignore */ } }, 250);
    } else {
      voiceError('Nie udało się uruchomić mikrofonu: ' + err.message);
    }
  }
}

/* ====================== wiring ====================== */

export function bindChatChrome() {
  el('chatTab').addEventListener('click', () => setChatOpen(!chatOpen));

  const input = el('composerInput');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = input.value;
      input.value = '';
      sendMessage(v, { spoken: false });     // typed in -> text only
    }
  });
  input.addEventListener('focus', () => el('textBubble').classList.add('focused'));
  input.addEventListener('blur', () => el('textBubble').classList.remove('focused'));

  const btn = el('voiceBtn');
  if (!SR) {
    btn.classList.add('unsupported');
    btn.title = 'Rozpoznawanie mowy wymaga Chrome';
  }
  btn.addEventListener('click', toggleListening);

  // Space toggles the mic when not typing.
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space') return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (!el('overlay').hidden) return;
    e.preventDefault();
    toggleListening();
  });
}

export async function loadChatHistory() {
  try {
    state.chat = await api.get('/api/messages');
  } catch { state.chat = []; }
  renderChat();
}
