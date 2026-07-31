/* Shared state, date helpers and the API client.

   The event cache is deliberately generous: navigation between day, week,
   month and year views re-renders from memory instead of refetching, so
   switching views and stepping through dates is instant. The network is
   only touched when the visible range falls outside what is already loaded,
   or after a mutation. */

export const API_BASE = "";   // same origin — the backend serves this page

export const CATEGORIES = [
  { id: 'habit',         label: 'Nawyk' },
  { id: 'entertainment', label: 'Rozrywka' },
  { id: 'meeting',       label: 'Spotkanie' },
  { id: 'important',     label: 'Ważne' },
  { id: 'focus',         label: 'Skupienie' }
];

export const CAT_COLOR = {
  habit: '#5FB88A', entertainment: '#A98CE0', meeting: '#5B9BE0',
  important: '#E0785B', focus: '#3FB8B0'
};

export const PX_PER_MIN = 1.35;
export const DAY_MIN = 24 * 60;
export const DAY_HEIGHT = DAY_MIN * PX_PER_MIN;
export const SNAP_MIN = 5;

export const PL_DOW_SHORT = ['Pon', 'Wt', 'Śr', 'Czw', 'Pt', 'Sob', 'Nd'];
export const PL_MONTHS = ['Styczeń', 'Luty', 'Marzec', 'Kwiecień', 'Maj', 'Czerwiec',
  'Lipiec', 'Sierpień', 'Wrzesień', 'Październik', 'Listopad', 'Grudzień'];

export const state = {
  events: [],        // every event currently cached, keyed by nothing — filtered per view
  reminders: [],
  notes: [],
  facts: [],
  activities: [],
  chat: [],
  view: 'day',       // day | week | month | year
  cursor: todayStr(),// the date the current view is anchored on
  editing: null,
  loadedFrom: null,
  loadedTo: null
};

/* ---------- dates ---------- */
export function pad2(n) { return String(n).padStart(2, '0'); }
export function fmtDate(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
export function todayStr() { return fmtDate(new Date()); }
export function parseDate(s) { return new Date(s + 'T00:00:00'); }
export function addDays(s, n) { const d = parseDate(s); d.setDate(d.getDate() + n); return fmtDate(d); }
export function addMonths(s, n) { const d = parseDate(s); d.setMonth(d.getMonth() + n); return fmtDate(d); }
export function toMinutes(hhmm) { const p = String(hhmm).split(':'); return (+p[0]) * 60 + (+p[1]); }
export function minutesToHHMM(min) {
  min = Math.max(0, Math.min(DAY_MIN - 1, Math.round(min)));
  return pad2(Math.floor(min / 60)) + ':' + pad2(min % 60);
}
export function nowHHMM() { const d = new Date(); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }

/** Monday-based start of the week containing `s`. */
export function startOfWeek(s) {
  const d = parseDate(s);
  const dow = (d.getDay() + 6) % 7;      // 0 = Monday
  d.setDate(d.getDate() - dow);
  return fmtDate(d);
}
export function startOfMonth(s) { const d = parseDate(s); d.setDate(1); return fmtDate(d); }
export function endOfMonth(s) { const d = parseDate(s); d.setMonth(d.getMonth() + 1, 0); return fmtDate(d); }
export function daysInMonth(year, month) { return new Date(year, month + 1, 0).getDate(); }

export function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

/* ---------- transport ---------- */
let connBanner = null;
export function showConn(show) {
  connBanner = connBanner || document.getElementById('connBanner');
  if (connBanner) connBanner.hidden = !show;
}

async function req(path, method, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API_BASE + path, opts);
  if (!r.ok) {
    const txt = await r.text().catch(() => '');
    throw new Error('API ' + r.status + ' ' + txt.slice(0, 120));
  }
  showConn(false);
  return r.status === 204 ? null : r.json();
}

async function guarded(fn) {
  try { return await fn(); }
  catch (e) { showConn(true); throw e; }
}

export const api = {
  get:  (p)    => guarded(() => req(p, 'GET')),
  post: (p, b) => guarded(() => req(p, 'POST', b)),
  put:  (p, b) => guarded(() => req(p, 'PUT', b)),
  del:  (p)    => guarded(() => req(p, 'DELETE'))
};

/* ---------- event cache ---------- */

/** Widest range any view could need around `cursor`, padded so ordinary
    navigation keeps hitting the cache instead of the network. */
function rangeFor(view, cursor) {
  if (view === 'year') {
    const y = parseDate(cursor).getFullYear();
    return [y + '-01-01', y + '-12-31'];
  }
  if (view === 'month') {
    return [addDays(startOfMonth(cursor), -14), addDays(endOfMonth(cursor), 14)];
  }
  return [addDays(cursor, -21), addDays(cursor, 21)];
}

export async function ensureEvents(view, cursor, force) {
  const [from, to] = rangeFor(view, cursor);
  const covered = !force && state.loadedFrom && state.loadedTo &&
                  state.loadedFrom <= from && state.loadedTo >= to;
  if (covered) return state.events;

  const wideFrom = from < (state.loadedFrom || from) ? from : (state.loadedFrom || from);
  const wideTo   = to   > (state.loadedTo   || to)   ? to   : (state.loadedTo   || to);
  const rows = await api.get(`/api/events?start=${wideFrom}&end=${wideTo}`);
  state.events = rows;
  state.loadedFrom = wideFrom;
  state.loadedTo = wideTo;
  return rows;
}

export function eventsOn(dateStr) {
  return state.events.filter(e => e.date === dateStr);
}

export function eventsBetween(fromStr, toStr) {
  return state.events.filter(e => e.date >= fromStr && e.date <= toStr);
}

/** Invalidate so the next ensureEvents refetches — used after the agent
    changes things behind our back. */
export function invalidateEvents() {
  state.loadedFrom = null;
  state.loadedTo = null;
}

/* ---------- misc ---------- */
let toastEl = null, toastTimer = null;
export function toast(msg) {
  toastEl = toastEl || document.getElementById('toast');
  if (!toastEl) return;
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 1600);
}
