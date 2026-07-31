/* Calendar: day / week / month / year views, with drag-to-move and resize.

   Two performance rules hold everywhere in here:

   1. Rendering never waits on the network. Views draw from the in-memory
      event cache, so switching view or stepping a day is a synchronous
      re-render.

   2. Dragging never re-renders. While a pointer is down we mutate the
      dragged element's own style directly and leave the rest of the DOM
      untouched; a single re-render happens on drop. The PUT is fired
      afterwards and the move is rolled back only if the server rejects it. */

import {
  state, api, CAT_COLOR, PX_PER_MIN, DAY_MIN, DAY_HEIGHT, SNAP_MIN,
  PL_DOW_SHORT, PL_MONTHS, pad2, fmtDate, todayStr, parseDate, addDays,
  toMinutes, minutesToHHMM, startOfWeek, startOfMonth, endOfMonth,
  eventsOn, eventsBetween, escapeHtml, ensureEvents, invalidateEvents, toast
} from './api.js';

let openEventModal = () => {};
export function setEventModalOpener(fn) { openEventModal = fn; }

const els = {};
export function initCalendarEls() {
  Object.assign(els, {
    title: document.getElementById('calTitle'),
    dateLabel: document.getElementById('calDateLabel'),
    switch: document.getElementById('viewSwitch'),
    dayScroll: document.getElementById('dayScroll'),
    ruler: document.getElementById('calRuler'),
    track: document.getElementById('calTrack'),
    weekWrap: document.getElementById('weekWrap'),
    weekHead: document.getElementById('weekHead'),
    weekScroll: document.getElementById('weekScroll'),
    weekRuler: document.getElementById('weekRuler'),
    weekCols: document.getElementById('weekCols'),
    monthWrap: document.getElementById('monthWrap'),
    monthDow: document.getElementById('monthDow'),
    monthGrid: document.getElementById('monthGrid'),
    yearScroll: document.getElementById('yearScroll'),
    yearGrid: document.getElementById('yearGrid')
  });
}

/* ====================== shared bits ====================== */

function fillRuler(el) {
  el.innerHTML = '';
  el.style.height = DAY_HEIGHT + 'px';
  for (let h = 0; h < 24; h++) {
    const d = document.createElement('div');
    d.className = 'cal-ruler-hour';
    d.style.top = (h * 60 * PX_PER_MIN) + 'px';
    d.textContent = pad2(h) + ':00';
    el.appendChild(d);
  }
}

function fillHourLines(el) {
  for (let h = 0; h < 24; h++) {
    const hl = document.createElement('div');
    hl.className = 'hour-line';
    hl.style.top = (h * 60 * PX_PER_MIN) + 'px';
    el.appendChild(hl);
  }
}

function addNowLine(el, dateStr) {
  if (dateStr !== todayStr()) return;
  const now = new Date();
  const line = document.createElement('div');
  line.className = 'now-line';
  line.style.top = ((now.getHours() * 60 + now.getMinutes()) * PX_PER_MIN) + 'px';
  el.appendChild(line);
}

/** Side-by-side columns for events that overlap in time. */
function layoutDayEvents(dayEvents) {
  const sorted = dayEvents.slice().sort((a, b) => toMinutes(a.start) - toMinutes(b.start));
  const clusters = [];
  let current = [], clusterEnd = -1;
  sorted.forEach(ev => {
    const s = toMinutes(ev.start), e = toMinutes(ev.end);
    if (current.length === 0 || s < clusterEnd) { current.push(ev); clusterEnd = Math.max(clusterEnd, e); }
    else { clusters.push(current); current = [ev]; clusterEnd = e; }
  });
  if (current.length) clusters.push(current);

  const out = [];
  clusters.forEach(cluster => {
    const colEnds = [], colOf = {};
    cluster.forEach(ev => {
      const s = toMinutes(ev.start);
      let placed = false;
      for (let c = 0; c < colEnds.length; c++) {
        if (colEnds[c] <= s) { colEnds[c] = toMinutes(ev.end); colOf[ev.id] = c; placed = true; break; }
      }
      if (!placed) { colEnds.push(toMinutes(ev.end)); colOf[ev.id] = colEnds.length - 1; }
    });
    cluster.forEach(ev => out.push({ ...ev, col: colOf[ev.id], totalCols: colEnds.length }));
  });
  return out;
}

function makeEventBlock(ev, opts = {}) {
  const color = CAT_COLOR[ev.category] || CAT_COLOR.important;
  const s = toMinutes(ev.start), e = toMinutes(ev.end);
  const block = document.createElement('div');
  block.className = 'cal-event';
  block.dataset.id = ev.id;
  block.style.top = (s * PX_PER_MIN) + 'px';
  block.style.height = Math.max((e - s) * PX_PER_MIN, 16) + 'px';
  block.style.background = color;

  const widthPct = 100 / (ev.totalCols || 1);
  const col = ev.col || 0;
  block.style.left = `calc(${col * widthPct}% + ${col ? 4 : 2}px)`;
  block.style.width = `calc(${widthPct}% - 7px)`;

  block.innerHTML =
    `<div class="resize-handle top"></div>` +
    `<div class="cal-event-title">${escapeHtml(ev.title)}</div>` +
    `<div class="cal-event-time">${ev.start}–${ev.end}</div>` +
    `<div class="resize-handle bottom"></div>`;

  bindEventDrag(block, ev, opts);
  return block;
}

/* ====================== persistence ====================== */

async function commitMove(ev, patch) {
  const before = { date: ev.date, start: ev.start, end: ev.end };
  const local = state.events.find(x => x.id === ev.id);
  if (local) Object.assign(local, patch);           // optimistic
  render();

  try {
    await api.put(`/api/events/${ev.id}`, patch);
  } catch (err) {
    if (local) Object.assign(local, before);        // roll back
    render();
    toast('Nie udało się zapisać zmiany');
  }
}

/* ====================== drag: move + resize ====================== */

function snap(min) { return Math.round(min / SNAP_MIN) * SNAP_MIN; }

/** Attaches move/resize behaviour to one event block.
    `opts.columnsOf()` returns the week columns so a drag can change day. */
function bindEventDrag(block, ev, opts) {
  block.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.stopPropagation();                 // don't start a create-drag underneath

    const mode = e.target.classList.contains('resize-handle')
      ? (e.target.classList.contains('top') ? 'resize-top' : 'resize-bottom')
      : 'move';

    // Always start from what the event is right now, not from the copy this
    // block was rendered with — otherwise a second drag computes against
    // stale coordinates and the event appears stuck on its first target.
    const live = state.events.find(x => x.id === ev.id) || ev;
    const origDate = live.date;

    const startY = e.clientY, startX = e.clientX;
    const origStart = toMinutes(live.start), origEnd = toMinutes(live.end);
    const duration = origEnd - origStart;
    let curStart = origStart, curEnd = origEnd, curDate = origDate;
    let moved = false;

    const cols = opts.columnsOf ? opts.columnsOf() : null;

    block.setPointerCapture(e.pointerId);

    const onMove = (me) => {
      const dy = me.clientY - startY;
      if (!moved && Math.abs(dy) < 3 && Math.abs(me.clientX - startX) < 3) return;
      if (!moved) { moved = true; block.classList.add('dragging'); }

      const dMin = snap(dy / PX_PER_MIN);

      if (mode === 'move') {
        curStart = Math.max(0, Math.min(DAY_MIN - duration, snap(origStart + dMin)));
        curEnd = curStart + duration;

        // Horizontal movement changes the day, but only in week view.
        // The block is translated rather than re-parented: moving a node
        // between columns re-inserts it in the DOM, which releases the
        // pointer capture and silently ends the drag.
        if (cols && cols.length) {
          const hit = cols.find(c => {
            const r = c.el.getBoundingClientRect();
            return me.clientX >= r.left && me.clientX <= r.right;
          });
          if (hit && hit.date !== curDate) {
            curDate = hit.date;
            cols.forEach(c => c.el.classList.toggle('drop-target', c.date === curDate));
          }
          const from = cols.findIndex(c => c.date === origDate);
          const to = cols.findIndex(c => c.date === curDate);
          if (from !== -1 && to !== -1) {
            const w = cols[0].el.getBoundingClientRect().width;
            block.style.transform = `translateX(${(to - from) * w}px)`;
          }
        }
      } else if (mode === 'resize-top') {
        curStart = Math.max(0, Math.min(origEnd - SNAP_MIN, snap(origStart + dMin)));
        curEnd = origEnd;
      } else {
        curEnd = Math.min(DAY_MIN, Math.max(origStart + SNAP_MIN, snap(origEnd + dMin)));
        curStart = origStart;
      }

      block.style.top = (curStart * PX_PER_MIN) + 'px';
      block.style.height = Math.max((curEnd - curStart) * PX_PER_MIN, 16) + 'px';
      const t = block.querySelector('.cal-event-time');
      if (t) t.textContent = minutesToHHMM(curStart) + '–' + minutesToHHMM(curEnd);
    };

    const onUp = () => {
      block.releasePointerCapture?.(e.pointerId);
      block.removeEventListener('pointermove', onMove);
      block.removeEventListener('pointerup', onUp);
      block.removeEventListener('pointercancel', onUp);
      block.classList.remove('dragging');
      block.style.transform = '';
      if (cols) cols.forEach(c => c.el.classList.remove('drop-target'));

      if (!moved) { openEventModal(ev.id); return; }   // a click, not a drag

      const patch = {
        date: curDate,
        start: minutesToHHMM(curStart),
        end: minutesToHHMM(curEnd)
      };
      if (patch.date === origDate && patch.start === live.start && patch.end === live.end) {
        render();
        return;
      }
      commitMove(live, patch);
    };

    block.addEventListener('pointermove', onMove);
    block.addEventListener('pointerup', onUp);
    block.addEventListener('pointercancel', onUp);
  });
}

/* ====================== drag on empty space to create ====================== */

function bindCreateDrag(track, dateStr) {
  track.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target !== track) return;
    const rect = track.getBoundingClientRect();
    const yToMin = (cy) => Math.max(0, Math.min(DAY_MIN, snap((cy - rect.top) / PX_PER_MIN)));

    const startMin = yToMin(e.clientY);
    let preview = null, moved = false;
    track.setPointerCapture(e.pointerId);

    const draw = (a, b) => {
      const s = Math.min(a, b), en = Math.max(a, b, s + SNAP_MIN);
      if (!preview) {
        preview = document.createElement('div');
        preview.className = 'cal-drag-preview';
        preview.style.left = '2px';
        preview.style.right = '3px';
        preview.innerHTML = '<span class="cal-drag-label"></span>';
        track.appendChild(preview);
      }
      preview.style.top = (s * PX_PER_MIN) + 'px';
      preview.style.height = Math.max((en - s) * PX_PER_MIN, 16) + 'px';
      preview.querySelector('.cal-drag-label').textContent =
        minutesToHHMM(s) + '–' + minutesToHHMM(en);
    };

    const onMove = (me) => {
      const cur = yToMin(me.clientY);
      if (Math.abs(cur - startMin) >= SNAP_MIN) moved = true;
      draw(startMin, cur);
    };

    const onUp = (ue) => {
      track.releasePointerCapture?.(e.pointerId);
      track.removeEventListener('pointermove', onMove);
      track.removeEventListener('pointerup', onUp);
      if (preview) preview.remove();
      const endMin = yToMin(ue.clientY);
      if (moved) {
        const s = Math.min(startMin, endMin);
        const en = Math.max(startMin, endMin, s + SNAP_MIN);
        openEventModal(null, s, en, dateStr);
      } else {
        openEventModal(null, startMin, null, dateStr);
      }
    };

    track.addEventListener('pointermove', onMove);
    track.addEventListener('pointerup', onUp);
  });
}

/* ====================== DAY ====================== */

function renderDay() {
  const d = parseDate(state.cursor);
  const s = d.toLocaleDateString('pl-PL', { weekday: 'long', day: 'numeric', month: 'long' });
  els.title.textContent = s.charAt(0).toUpperCase() + s.slice(1);
  els.dateLabel.textContent = d.toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit', year: 'numeric' });

  fillRuler(els.ruler);
  els.track.innerHTML = '';
  els.track.style.height = DAY_HEIGHT + 'px';
  fillHourLines(els.track);

  const dayEvents = eventsOn(state.cursor);
  if (!dayEvents.length) {
    const empty = document.createElement('div');
    empty.className = 'cal-empty';
    empty.textContent = 'Brak wydarzeń — kliknij siatkę, aby dodać.';
    Object.assign(empty.style, { position: 'absolute', top: '40px', left: '0', right: '0' });
    els.track.appendChild(empty);
  }

  layoutDayEvents(dayEvents).forEach(ev => els.track.appendChild(makeEventBlock(ev, {})));
  addNowLine(els.track, state.cursor);
}

/* ====================== WEEK ====================== */

function renderWeek() {
  const from = startOfWeek(state.cursor);
  const days = Array.from({ length: 7 }, (_, i) => addDays(from, i));
  const to = days[6];

  const a = parseDate(from), b = parseDate(to);
  els.title.textContent = `${a.getDate()} ${PL_MONTHS[a.getMonth()].toLowerCase()} – ${b.getDate()} ${PL_MONTHS[b.getMonth()].toLowerCase()}`;
  els.dateLabel.textContent = `tydzień ${from} → ${to}`;

  // header
  els.weekHead.innerHTML = '<div class="spacer"></div>';
  days.forEach(ds => {
    const d = parseDate(ds);
    const cell = document.createElement('div');
    cell.className = 'wh-day' + (ds === todayStr() ? ' is-today' : '');
    cell.innerHTML = `<div class="wh-name">${PL_DOW_SHORT[(d.getDay() + 6) % 7]}</div>` +
                     `<div class="wh-num">${d.getDate()}</div>`;
    cell.addEventListener('click', () => { state.cursor = ds; setView('day'); });
    els.weekHead.appendChild(cell);
  });

  fillRuler(els.weekRuler);
  els.weekCols.innerHTML = '';
  els.weekCols.style.height = DAY_HEIGHT + 'px';

  const colRefs = [];
  const columnsOf = () => colRefs;

  days.forEach(ds => {
    const col = document.createElement('div');
    col.className = 'week-col' + (ds === todayStr() ? ' is-today' : '');
    col.style.height = DAY_HEIGHT + 'px';
    fillHourLines(col);
    bindCreateDrag(col, ds);
    els.weekCols.appendChild(col);
    colRefs.push({ date: ds, el: col });

    layoutDayEvents(eventsOn(ds)).forEach(ev => col.appendChild(makeEventBlock(ev, { columnsOf })));
    addNowLine(col, ds);
  });
}

/* ====================== MONTH ====================== */

function renderMonth() {
  const first = startOfMonth(state.cursor);
  const d = parseDate(first);
  els.title.textContent = `${PL_MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  els.dateLabel.textContent = `${first} → ${endOfMonth(state.cursor)}`;

  els.monthDow.innerHTML = PL_DOW_SHORT.map(x => `<span>${x}</span>`).join('');

  const gridStart = startOfWeek(first);
  const cells = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const month = d.getMonth();

  els.monthGrid.innerHTML = '';
  cells.forEach(ds => {
    const cd = parseDate(ds);
    const cell = document.createElement('div');
    cell.className = 'month-cell' +
      (cd.getMonth() !== month ? ' other-month' : '') +
      (ds === todayStr() ? ' is-today' : '');
    cell.dataset.date = ds;

    const num = document.createElement('div');
    num.className = 'mc-num';
    num.textContent = cd.getDate();
    cell.appendChild(num);

    const dayEvents = eventsOn(ds).sort((x, y) => toMinutes(x.start) - toMinutes(y.start));
    dayEvents.slice(0, 3).forEach(ev => {
      const chip = document.createElement('div');
      chip.className = 'mc-chip';
      chip.style.background = CAT_COLOR[ev.category] || CAT_COLOR.important;
      chip.textContent = `${ev.start} ${ev.title}`;
      chip.title = `${ev.start}–${ev.end} ${ev.title}`;
      bindMonthChipDrag(chip, ev);
      cell.appendChild(chip);
    });
    if (dayEvents.length > 3) {
      const more = document.createElement('div');
      more.className = 'mc-more';
      more.textContent = `+${dayEvents.length - 3} więcej`;
      cell.appendChild(more);
    }

    cell.addEventListener('click', (e) => {
      if (e.target.classList.contains('mc-chip')) return;
      state.cursor = ds;
      setView('day');
    });
    els.monthGrid.appendChild(cell);
  });
}

/** In month view a drag moves an event to another day, keeping its time. */
function bindMonthChipDrag(chip, ev) {
  chip.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    const live = state.events.find(x => x.id === ev.id) || ev;
    const startX = e.clientX, startY = e.clientY;
    let moved = false, targetDate = live.date;
    chip.setPointerCapture(e.pointerId);

    const onMove = (me) => {
      if (!moved && Math.abs(me.clientX - startX) < 4 && Math.abs(me.clientY - startY) < 4) return;
      if (!moved) { moved = true; chip.classList.add('dragging'); }

      const under = document.elementFromPoint(me.clientX, me.clientY);
      const cell = under && under.closest('.month-cell');
      els.monthGrid.querySelectorAll('.drop-target').forEach(c => c.classList.remove('drop-target'));
      if (cell) { cell.classList.add('drop-target'); targetDate = cell.dataset.date; }
    };

    const onUp = () => {
      chip.releasePointerCapture?.(e.pointerId);
      chip.removeEventListener('pointermove', onMove);
      chip.removeEventListener('pointerup', onUp);
      chip.classList.remove('dragging');
      els.monthGrid.querySelectorAll('.drop-target').forEach(c => c.classList.remove('drop-target'));

      if (!moved) { openEventModal(ev.id); return; }
      if (targetDate === live.date) { render(); return; }
      commitMove(live, { date: targetDate, start: live.start, end: live.end });
    };

    chip.addEventListener('pointermove', onMove);
    chip.addEventListener('pointerup', onUp);
  });
}

/* ====================== YEAR (scrollable months) ====================== */

function renderYear() {
  const year = parseDate(state.cursor).getFullYear();
  els.title.textContent = String(year);
  els.dateLabel.textContent = `${year}-01-01 → ${year}-12-31`;

  const counts = {};
  eventsBetween(`${year}-01-01`, `${year}-12-31`).forEach(e => {
    counts[e.date] = (counts[e.date] || 0) + 1;
  });

  const today = todayStr();
  const frag = document.createDocumentFragment();

  for (let m = 0; m < 12; m++) {
    const firstOfMonth = `${year}-${pad2(m + 1)}-01`;
    const total = Object.keys(counts).filter(d => d.startsWith(`${year}-${pad2(m + 1)}`))
      .reduce((n, d) => n + counts[d], 0);

    const card = document.createElement('div');
    card.className = 'ym-card' + (m === new Date().getMonth() && year === new Date().getFullYear() ? ' is-current' : '');

    const title = document.createElement('div');
    title.className = 'ym-title';
    title.innerHTML = `<span>${PL_MONTHS[m]}</span><span class="ym-count">${total || ''}</span>`;
    title.addEventListener('click', () => { state.cursor = firstOfMonth; setView('month'); });
    card.appendChild(title);

    const dow = document.createElement('div');
    dow.className = 'ym-dow';
    dow.innerHTML = PL_DOW_SHORT.map(x => `<span>${x[0]}</span>`).join('');
    card.appendChild(dow);

    const grid = document.createElement('div');
    grid.className = 'ym-days';
    const lead = (parseDate(firstOfMonth).getDay() + 6) % 7;
    for (let i = 0; i < lead; i++) {
      const sp = document.createElement('div');
      sp.className = 'ym-day empty';
      grid.appendChild(sp);
    }
    const dim = new Date(year, m + 1, 0).getDate();
    for (let day = 1; day <= dim; day++) {
      const ds = `${year}-${pad2(m + 1)}-${pad2(day)}`;
      const cell = document.createElement('div');
      cell.className = 'ym-day' + (ds === today ? ' is-today' : '') + (counts[ds] ? ' has-events' : '');
      cell.textContent = day;
      cell.title = counts[ds] ? `${counts[ds]} wydarzeń` : '';
      cell.addEventListener('click', () => { state.cursor = ds; setView('day'); });
      grid.appendChild(cell);
    }
    card.appendChild(grid);
    frag.appendChild(card);
  }

  els.yearGrid.innerHTML = '';
  els.yearGrid.appendChild(frag);
}

/* ====================== view plumbing ====================== */

const VIEWS = {
  day:   () => [els.dayScroll],
  week:  () => [els.weekWrap],
  month: () => [els.monthWrap],
  year:  () => [els.yearScroll]
};

export function render() {
  Object.values(VIEWS).forEach(get => get().forEach(el => { el.hidden = true; }));
  VIEWS[state.view]().forEach(el => { el.hidden = false; });

  els.switch.querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', b.dataset.view === state.view));

  if (state.view === 'day') renderDay();
  else if (state.view === 'week') renderWeek();
  else if (state.view === 'month') renderMonth();
  else renderYear();
}

export async function setView(view) {
  state.view = view;
  render();                                  // instant, from cache
  await ensureEvents(view, state.cursor);    // top up if the range grew
  render();
  if (view === 'day') scrollToNow(els.dayScroll);
  if (view === 'week') scrollToNow(els.weekScroll);
}

export function scrollToNow(scroller) {
  const now = new Date();
  const min = now.getHours() * 60 + now.getMinutes();
  scroller.scrollTop = Math.max(0, min * PX_PER_MIN - 160);
}

export async function step(dir) {
  if (state.view === 'day') state.cursor = addDays(state.cursor, dir);
  else if (state.view === 'week') state.cursor = addDays(state.cursor, 7 * dir);
  else if (state.view === 'month') {
    const d = parseDate(startOfMonth(state.cursor));
    d.setMonth(d.getMonth() + dir);
    state.cursor = fmtDate(d);
  } else {
    const d = parseDate(state.cursor);
    d.setFullYear(d.getFullYear() + dir);
    state.cursor = fmtDate(d);
  }
  render();
  await ensureEvents(state.view, state.cursor);
  render();
}

export async function goToday() {
  state.cursor = todayStr();
  render();
  await ensureEvents(state.view, state.cursor);
  render();
  if (state.view === 'day') scrollToNow(els.dayScroll);
  if (state.view === 'week') scrollToNow(els.weekScroll);
}

export async function reloadEvents() {
  invalidateEvents();
  await ensureEvents(state.view, state.cursor, true);
  render();
}

export function bindCalendarChrome() {
  els.switch.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-view]');
    if (btn) setView(btn.dataset.view);
  });
  document.getElementById('calPrev').addEventListener('click', () => step(-1));
  document.getElementById('calNext').addEventListener('click', () => step(1));
  document.getElementById('calToday').addEventListener('click', goToday);
  document.getElementById('addEventBtn').addEventListener('click',
    () => openEventModal(null, 9 * 60, null, state.cursor));

  bindCreateDrag(els.track, null);   // day view: null means "use state.cursor"

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const map = { d: 'day', w: 'week', m: 'month', r: 'year' };
    if (map[e.key.toLowerCase()]) { setView(map[e.key.toLowerCase()]); return; }
    if (e.key === 'ArrowLeft') step(-1);
    if (e.key === 'ArrowRight') step(1);
    if (e.key.toLowerCase() === 't') goToday();
  });
}
