/* Reminders, notes, the memory panel, and the shared modal.

   Everything here is direct REST — no model involved — so creating,
   editing and deleting are immediate. */

import {
  state, api, CATEGORIES, CAT_COLOR, todayStr, toMinutes, escapeHtml, toast
} from './api.js';
import { reloadEvents } from './calendar.js';

const el = (id) => document.getElementById(id);

let overlay, modalBox, modalTitle, modalFields, errorText,
    saveBtn, cancelBtn, deleteBtn;
let modalKind = null;

export function initPanelEls() {
  overlay = el('overlay');
  modalBox = el('modalBox');
  modalTitle = el('modalTitle');
  modalFields = el('modalFields');
  errorText = el('errorText');
  saveBtn = el('saveBtn');
  cancelBtn = el('cancelBtn');
  deleteBtn = el('deleteBtn');
}

/* ====================== reminders ====================== */

export function renderReminders() {
  const list = el('reminderList');
  const today = todayStr();
  const todays = state.reminders
    .filter(r => r.date === today && !r.done)
    .sort((a, b) => toMinutes(a.time) - toMinutes(b.time));

  list.innerHTML = '';
  if (!todays.length) {
    list.innerHTML = '<div class="side-empty">Brak przypomnień na dziś.</div>';
    return;
  }
  todays.forEach(r => {
    const item = document.createElement('div');
    item.className = 'rem-item';
    item.innerHTML =
      `<div class="rem-check${r.done ? ' done' : ''}"></div>` +
      `<div class="rem-body"><div class="rem-time">${escapeHtml(r.time)}</div>` +
      `<div class="rem-text">${escapeHtml(r.text)}</div></div>`;
    item.querySelector('.rem-check').addEventListener('click', async (e) => {
      e.stopPropagation();
      await api.put(`/api/reminders/${r.id}`, { done: !r.done });
      await reloadReminders();
    });
    item.addEventListener('click', () => openReminderModal(r.id));
    list.appendChild(item);
  });
}

export async function reloadReminders() {
  try { state.reminders = await api.get('/api/reminders'); } catch { /* banner shown */ }
  renderReminders();
}

/* ====================== notes ====================== */

function formatNoteTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit' });
}

export function renderNotes() {
  const list = el('noteList');
  list.innerHTML = '';
  if (!state.notes.length) {
    list.innerHTML = '<div class="side-empty">Brak notatek.</div>';
    return;
  }
  state.notes.forEach(n => {
    const item = document.createElement('div');
    item.className = 'note-item';
    item.innerHTML =
      `<div class="note-head"><div class="note-title">${escapeHtml(n.title || 'Bez tytułu')}</div>` +
      `<div class="note-time">${formatNoteTime(n.updated_at)}</div></div>` +
      `<div class="note-snippet">${escapeHtml(n.content || '')}</div>`;
    item.addEventListener('click', () => openNoteModal(n.id));
    list.appendChild(item);
  });
}

export async function reloadNotes() {
  try { state.notes = await api.get('/api/notes'); } catch { /* banner shown */ }
  renderNotes();
}

/* The assistant's memory (facts, routines, activity history) lives entirely
   in the backend and is injected into its prompt. It is deliberately not
   surfaced in the UI. */

/* ====================== modal ====================== */

function fieldHTML(label, inner) {
  return `<div class="field"><label>${label}</label>${inner}</div>`;
}

export function openEventModal(id, prefillStart, prefillEnd, dateStr) {
  modalKind = 'event';
  const ev = id ? state.events.find(e => e.id === id) : null;
  state.editing = ev ? { ...ev } : null;

  const date = ev ? ev.date : (dateStr || state.cursor);
  const start = ev ? ev.start
    : (prefillStart != null ? minutes(prefillStart) : '09:00');
  const end = ev ? ev.end
    : (prefillEnd != null ? minutes(prefillEnd) : minutes((prefillStart != null ? prefillStart : 540) + 60));

  let selectedCat = ev ? ev.category : 'important';

  modalTitle.textContent = ev ? 'Edytuj wydarzenie' : 'Nowe wydarzenie';
  modalBox.classList.remove('wide');
  modalFields.innerHTML =
    fieldHTML('Tytuł', `<input id="f_title" type="text" value="${escapeHtml(ev ? ev.title : '')}" placeholder="np. Trening">`) +
    `<div class="field-row">` +
      fieldHTML('Data', `<input id="f_date" type="date" value="${date}">`) +
      fieldHTML('Od', `<input id="f_start" type="time" value="${start}">`) +
      fieldHTML('Do', `<input id="f_end" type="time" value="${end}">`) +
    `</div>` +
    fieldHTML('Kategoria', `<div class="cat-picker" id="f_cats"></div>`) +
    fieldHTML('Opis', `<textarea id="f_desc" style="min-height:60px" placeholder="opcjonalnie">${escapeHtml(ev ? ev.description : '')}</textarea>`);

  const picker = el('f_cats');
  CATEGORIES.forEach(c => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'cat-chip' + (c.id === selectedCat ? ' active' : '');
    chip.textContent = c.label;
    if (c.id === selectedCat) chip.style.background = CAT_COLOR[c.id];
    chip.addEventListener('click', () => {
      selectedCat = c.id;
      picker.querySelectorAll('.cat-chip').forEach((b, i) => {
        const on = CATEGORIES[i].id === selectedCat;
        b.classList.toggle('active', on);
        b.style.background = on ? CAT_COLOR[CATEGORIES[i].id] : '';
      });
    });
    picker.appendChild(chip);
  });

  saveBtn.onclick = async () => {
    const payload = {
      title: el('f_title').value.trim(),
      category: selectedCat,
      date: el('f_date').value,
      start: el('f_start').value,
      end: el('f_end').value,
      description: el('f_desc').value.trim()
    };
    if (!payload.title) return fail('Podaj tytuł.');
    if (!payload.date || !payload.start || !payload.end) return fail('Uzupełnij datę i godziny.');
    if (toMinutes(payload.end) <= toMinutes(payload.start)) return fail('Koniec musi być po początku.');
    try {
      if (ev) await api.put(`/api/events/${ev.id}`, payload);
      else await api.post('/api/events', payload);
      closeModal();
      await reloadEvents();
    } catch (e) { fail('Nie udało się zapisać.'); }
  };

  deleteBtn.hidden = !ev;
  deleteBtn.onclick = async () => {
    if (!ev) return;
    try {
      await api.del(`/api/events/${ev.id}`);
      closeModal();
      await reloadEvents();
    } catch { fail('Nie udało się usunąć.'); }
  };

  show();
  el('f_title').focus();
}

function minutes(m) {
  m = Math.max(0, Math.min(24 * 60 - 1, Math.round(m)));
  return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}

export function openReminderModal(id) {
  modalKind = 'reminder';
  const r = id ? state.reminders.find(x => x.id === id) : null;
  modalTitle.textContent = r ? 'Edytuj przypomnienie' : 'Nowe przypomnienie';
  modalBox.classList.remove('wide');
  modalFields.innerHTML =
    fieldHTML('Treść', `<input id="f_text" type="text" value="${escapeHtml(r ? r.text : '')}" placeholder="np. Zadzwonić do lekarza">`) +
    `<div class="field-row">` +
      fieldHTML('Data', `<input id="f_date" type="date" value="${r ? r.date : todayStr()}">`) +
      fieldHTML('Godzina', `<input id="f_time" type="time" value="${r ? r.time : '12:00'}">`) +
    `</div>`;

  saveBtn.onclick = async () => {
    const payload = {
      text: el('f_text').value.trim(),
      date: el('f_date').value,
      time: el('f_time').value
    };
    if (!payload.text) return fail('Podaj treść.');
    try {
      if (r) await api.put(`/api/reminders/${r.id}`, payload);
      else await api.post('/api/reminders', payload);
      closeModal();
      await reloadReminders();
    } catch { fail('Nie udało się zapisać.'); }
  };

  deleteBtn.hidden = !r;
  deleteBtn.onclick = async () => {
    if (!r) return;
    await api.del(`/api/reminders/${r.id}`);
    closeModal();
    await reloadReminders();
  };

  show();
  el('f_text').focus();
}

export function openNoteModal(id) {
  modalKind = 'note';
  const n = id ? state.notes.find(x => x.id === id) : null;
  modalTitle.textContent = n ? 'Notatka' : 'Nowa notatka';
  modalBox.classList.add('wide');
  modalFields.innerHTML =
    fieldHTML('Tytuł', `<input id="f_title" type="text" value="${escapeHtml(n ? n.title : '')}" placeholder="Tytuł">`) +
    fieldHTML('Treść', `<textarea id="f_content" placeholder="Pisz…">${escapeHtml(n ? n.content : '')}</textarea>`);

  saveBtn.onclick = async () => {
    const payload = {
      title: el('f_title').value.trim(),
      content: el('f_content').value
    };
    try {
      if (n) await api.put(`/api/notes/${n.id}`, payload);
      else await api.post('/api/notes', payload);
      closeModal();
      await reloadNotes();
    } catch { fail('Nie udało się zapisać.'); }
  };

  deleteBtn.hidden = !n;
  deleteBtn.onclick = async () => {
    if (!n) return;
    await api.del(`/api/notes/${n.id}`);
    closeModal();
    await reloadNotes();
  };

  show();
  el('f_title').focus();
}

function show() { errorText.textContent = ''; overlay.hidden = false; }
function fail(msg) { errorText.textContent = msg; }

export function closeModal() {
  overlay.hidden = true;
  modalKind = null;
  state.editing = null;
  modalBox.classList.remove('wide');
}

export function bindPanelChrome() {
  el('addReminderBtn').addEventListener('click', () => openReminderModal(null));
  el('addNoteBtn').addEventListener('click', () => openNoteModal(null));

  cancelBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !overlay.hidden) closeModal();
    if (e.key === 'Enter' && !overlay.hidden && modalKind !== 'note' &&
        e.target.tagName !== 'TEXTAREA') {
      saveBtn.click();
    }
  });
}
