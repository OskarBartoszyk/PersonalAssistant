/* Bootstrap: wire the modules together and do the first paint.

   The initial load is ordered so the calendar appears as soon as its data
   arrives, rather than waiting on chat history or memory. */

import { state, ensureEvents, todayStr } from './api.js';
import {
  initCalendarEls, bindCalendarChrome, setEventModalOpener,
  render, setView, scrollToNow
} from './calendar.js';
import {
  initPanelEls, bindPanelChrome, openEventModal,
  reloadReminders, reloadNotes
} from './panels.js';
import { bindChatChrome, loadChatHistory, setChatOpen } from './chat.js';

initCalendarEls();
initPanelEls();

// calendar.js needs the modal but panels.js needs the calendar, so the
// opener is injected here instead of importing in a cycle.
setEventModalOpener(openEventModal);

bindCalendarChrome();
bindPanelChrome();
bindChatChrome();

async function boot() {
  state.cursor = todayStr();
  state.view = 'day';

  try {
    await ensureEvents(state.view, state.cursor, true);
  } catch { /* connection banner already shown */ }
  render();
  scrollToNow(document.getElementById('dayScroll'));

  // Everything else can populate behind the first paint.
  reloadReminders();
  reloadNotes();
  loadChatHistory();

  setChatOpen(true);
}

boot();

// Keep the "now" line honest without re-rendering constantly.
setInterval(() => {
  if (state.view === 'day' || state.view === 'week') render();
}, 60_000);
