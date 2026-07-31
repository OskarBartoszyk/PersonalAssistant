"""
The assistant — Ollama tool-calling loop, tuned hard for latency.

Profiling the original loop on gemma4:e4b showed where the time actually went:

    prompt processing   150-225 tok/s   (never the bottleneck)
    generation           12.5 tok/s     (the entire bottleneck)
    output per step     ~357 tokens     of which ~350 was hidden reasoning

So a single "plan my workout" request cost 3 model round trips at ~30s each.
Four changes bring that down to roughly one round trip:

1. think=False. gemma4:e4b is a reasoning model; it was emitting ~1200
   characters of thinking and zero content just to pick one tool. Disabling
   it cut output 514 -> 106 tokens AND made the model call the correct
   mutating tool directly instead of a redundant read first.

2. Context injection. Today's and tomorrow's events, learned facts, routines
   and preferences go straight into the system prompt. The model almost never
   needs to spend a 30s round trip on get_calendar_events to answer or plan.

3. Python-authored confirmations. When a turn consists purely of mutations,
   the receipt ("Dodalem: Trening, 18:00-18:40") is generated in Python rather
   than by asking the model for one more 30s reply. This is also strictly more
   truthful: the confirmation is built from what the database actually did, so
   the model cannot claim to have scheduled something it did not.

4. keep_alive=-1 plus a warm-up ping at startup, so the 9.6 GB model is never
   paged out and no request pays the ~10s cold-load.

Streaming is exposed through run_agent_stream, which emits status frames while
tools execute so the UI has something to show during generation.
"""

import datetime
import json
import re

import ollama

import db

MODEL = "gemma4:e4b"

# temperature=0 keeps tool arguments deterministic; num_predict is a safety
# cap, comfortably above the ~106 tokens a think=False step actually needs.
OPTIONS = {
    "temperature": 0,
    "top_p": 0.9,
    "num_predict": 500,
    "num_ctx": 8192,
}

# -1 means "never unload". Without it Ollama evicts the model and the next
# request pays ~10s of cold start.
KEEP_ALIVE = -1

MAX_STEPS = 4

PL_WEEKDAYS = ["poniedzialek", "wtorek", "sroda", "czwartek", "piatek", "sobota", "niedziela"]

# Tools that only read. If a turn touches one of these we must go back to the
# model so it can phrase an answer; pure-mutation turns are confirmed in Python.
READ_TOOLS = {
    "get_calendar_events",
    "find_free_slots",
    "get_reminders",
    "get_notes",
    "recall_facts",
    "get_activity_stats",
    "get_routines",
}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_calendar_events(date=None, start_date=None, end_date=None):
    return db.get_events(date=date, start_date=start_date, end_date=end_date)


def create_calendar_event(title, category, date, start, end, description=""):
    ev = db.create_event(title, category, date, start, end, description)
    # Habits are the things worth tracking over time, so they feed the
    # activity log automatically — that is what powers "how long have I
    # been going to the gym".
    if ev and category == "habit":
        db.log_activity(title, date, db.to_min(end) - db.to_min(start), ev["id"])
    return ev


def update_calendar_event(id, title=None, category=None, date=None, start=None,
                          end=None, description=None):
    ev = db.update_event(id, title=title, category=category, date=date,
                         start=start, end=end, description=description)
    return ev or {"error": f"no event with id {id}"}


def delete_calendar_event(id):
    return {"deleted": db.delete_event(id)}


def find_free_slots(date, duration_minutes, work_start="08:00", work_end="22:00"):
    return db.find_free_slots(date, duration_minutes, work_start, work_end)


def get_reminders(date=None):
    return db.get_reminders(date)


def create_reminder(text, date, time):
    return db.create_reminder(text, date, time)


def update_reminder(id, text=None, date=None, time=None, done=None):
    r = db.update_reminder(id, text=text, date=date, time=time, done=done)
    return r or {"error": f"no reminder with id {id}"}


def delete_reminder(id):
    return {"deleted": db.delete_reminder(id)}


def get_notes():
    return db.get_notes()


def create_note(title="", content=""):
    return db.create_note(title, content)


def remember_fact(key, value, category="other"):
    """Durable memory: hobbies, standing preferences, people, goals."""
    return db.remember_fact(key, value, category)


def recall_facts(query=None, category=None):
    return db.search_facts(query) if query else db.get_facts(category)


def get_activity_stats(activity):
    """How long the user has been doing something, plus streaks and totals."""
    return db.activity_stats(activity)


def log_activity(activity, date, minutes=0):
    return db.log_activity(activity, date, minutes)


def create_routine(title, category, default_start, duration_minutes, weekdays):
    return db.create_routine(title, category, default_start, duration_minutes, weekdays)


def get_routines():
    return db.get_routines()


TOOL_REGISTRY = {
    "get_calendar_events": get_calendar_events,
    "create_calendar_event": create_calendar_event,
    "update_calendar_event": update_calendar_event,
    "delete_calendar_event": delete_calendar_event,
    "find_free_slots": find_free_slots,
    "get_reminders": get_reminders,
    "create_reminder": create_reminder,
    "update_reminder": update_reminder,
    "delete_reminder": delete_reminder,
    "get_notes": get_notes,
    "create_note": create_note,
    "remember_fact": remember_fact,
    "recall_facts": recall_facts,
    "get_activity_stats": get_activity_stats,
    "log_activity": log_activity,
    "create_routine": create_routine,
    "get_routines": get_routines,
}

# Descriptions are deliberately terse — every character here is prefill cost
# on each of the model's steps.
CATS = ["habit", "entertainment", "meeting", "important", "focus"]
_D = {"type": "string"}
_I = {"type": "integer"}


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)},
    }}


TOOLS = [
    _fn("create_calendar_event", "Add an event to the calendar.",
        {"title": _D, "category": {"type": "string", "enum": CATS},
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "start": {"type": "string", "description": "HH:MM"},
         "end": {"type": "string", "description": "HH:MM"}, "description": _D},
        ["title", "category", "date", "start", "end"]),
    _fn("update_calendar_event", "Change an existing event. Pass only changed fields.",
        {"id": _D, "title": _D, "category": {"type": "string", "enum": CATS},
         "date": _D, "start": _D, "end": _D, "description": _D}, ["id"]),
    _fn("delete_calendar_event", "Remove an event by id.", {"id": _D}, ["id"]),
    _fn("get_calendar_events", "Read events for a day or range. Today/tomorrow are already in context.",
        {"date": _D, "start_date": _D, "end_date": _D}),
    _fn("find_free_slots", "Find gaps of at least duration_minutes on a date.",
        {"date": _D, "duration_minutes": _I, "work_start": _D, "work_end": _D},
        ["date", "duration_minutes"]),
    _fn("create_reminder", "Add a reminder.",
        {"text": _D, "date": _D, "time": _D}, ["text", "date", "time"]),
    _fn("get_reminders", "Read reminders, optionally for one date.", {"date": _D}),
    _fn("update_reminder", "Change a reminder or mark it done.",
        {"id": _D, "text": _D, "date": _D, "time": _D, "done": {"type": "boolean"}}, ["id"]),
    _fn("delete_reminder", "Remove a reminder by id.", {"id": _D}, ["id"]),
    _fn("create_note", "Save a note.", {"title": _D, "content": _D}),
    _fn("get_notes", "Read all notes.", {}),
    _fn("remember_fact",
        "Store a lasting fact about the user: hobby, standing preference, person, goal. "
        "Use whenever they state something durable.",
        {"key": {"type": "string", "description": "short snake_case id, e.g. gym_duration"},
         "value": _D,
         "category": {"type": "string", "enum": ["hobby", "preference", "routine", "person", "goal", "health", "work", "other"]}},
        ["key", "value", "category"]),
    _fn("recall_facts", "Look up remembered facts by text or category.",
        {"query": _D, "category": _D}),
    _fn("get_activity_stats",
        "How long the user has been doing an activity: start date, totals, streaks.",
        {"activity": _D}, ["activity"]),
    _fn("log_activity", "Record that an activity happened on a date.",
        {"activity": _D, "date": _D, "minutes": _I}, ["activity", "date"]),
    _fn("create_routine", "Save a recurring weekly block the assistant can schedule.",
        {"title": _D, "category": {"type": "string", "enum": CATS},
         "default_start": _D, "duration_minutes": _I,
         "weekdays": {"type": "string", "description": "comma list, 0=Mon..6=Sun"}},
        ["title", "category", "default_start", "duration_minutes", "weekdays"]),
    _fn("get_routines", "Read saved weekly routines.", {}),
]


# ---------------------------------------------------------------------------
# Context injection — the single biggest structural win
# ---------------------------------------------------------------------------

def _fmt_events(events):
    if not events:
        return "  (nic)"
    return "\n".join(
        f"  [{e['id']}] {e['start']}-{e['end']} {e['title']} ({e['category']})"
        for e in events
    )


# Static half of the system prompt. This never changes between requests, so
# together with the tool schemas it forms a ~1300 token prefix that Ollama's
# KV cache reuses across calls. Measured: a cached prefix takes prefill from
# 12.8s to 0.0s, turning a 22s request into a ~3s one. Nothing volatile —
# no clock, no event data — may appear in here, or the cache misses every time.
STATIC_RULES = """Jestes osobistym asystentem kalendarza. Odpowiadasz krotko, po polsku, bez markdown i bez emoji.

ZASADY:
- Sekcja STAN NA TERAZ (na koncu) jest zawsze aktualna i nadrzedna wobec historii rozmowy.
  Nie wywoluj get_calendar_events dla dzis ani jutra — masz je ponizej.
- Gdy uzytkownik prosi o zaplanowanie czegos, od razu wywolaj create_calendar_event
  dla kazdego elementu. Nie pytaj o zgode na plan, ktory da sie ulozyc.
- Nigdy nie pisz, ze cos zrobiles, jesli nie wywolales narzedzia w tej turze.
- Gdy uzytkownik podaje trwala informacje o sobie (hobby, stala preferencja, cel,
  osoba), zapisz ja przez remember_fact.
- Kategorie: habit, entertainment, meeting, important, focus.
- Daty YYYY-MM-DD, godziny HH:MM.
- Brakujace szczegoly przyjmij rozsadnie (wizyta=1h, trening=1h jesli nie wiesz)
  i napisz jakie zalozenie przyjales. Pytaj tylko gdy naprawde nie da sie dzialac."""


def build_context_block():
    """The volatile tail: clock, today's and tomorrow's plan, learned memory.

    Deliberately kept last in the prompt and as small as possible — every
    token here is re-prefilled on each request because it legitimately
    changes, while everything above it stays cached.
    """
    now = datetime.datetime.now(db.TZ)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    weekday = PL_WEEKDAYS[now.weekday()]

    facts = db.get_facts()
    facts_block = "\n".join(f"  {f['key']}: {f['value']}" for f in facts) or "  (nic)"

    routines = db.get_routines()
    routines_block = "\n".join(
        f"  {r['title']} {r['default_start']} ({r['duration_minutes']}min) dni={r['weekdays']}"
        for r in routines
    ) or "  (nic)"

    prefs = db.get_preferences()
    prefs_block = "\n".join(f"  {k}: {v}" for k, v in prefs.items()) or "  (nic)"

    return f"""STAN NA TERAZ:
{weekday} {today}, godzina {now.strftime('%H:%M')}. Jutro to {tomorrow}.

PLAN NA DZIS ({today}):
{_fmt_events(db.get_events(date=today))}

PLAN NA JUTRO ({tomorrow}):
{_fmt_events(db.get_events(date=tomorrow))}

CO WIEM O UZYTKOWNIKU:
{facts_block}

RUTYNY:
{routines_block}

PREFERENCJE:
{prefs_block}"""


def build_system_prompt():
    """Full prompt: cached static prefix first, volatile tail last."""
    return STATIC_RULES + "\n\n" + build_context_block()


# ---------------------------------------------------------------------------
# Python-authored confirmations
# ---------------------------------------------------------------------------

def _describe(name, args, result):
    """Builds a receipt from what the database actually returned, so the
    confirmation can never overstate what happened."""
    if isinstance(result, dict) and result.get("error"):
        return f"Nie udalo sie: {result['error']}"

    if name == "create_calendar_event" and isinstance(result, dict):
        return f"Dodalem: {result['title']}, {result['date']} {result['start']}-{result['end']}."
    if name == "update_calendar_event" and isinstance(result, dict):
        return f"Zmienilem: {result['title']}, {result['date']} {result['start']}-{result['end']}."
    if name == "delete_calendar_event":
        return "Usunalem wydarzenie." if result.get("deleted") else "Nie znalazlem wydarzenia."
    if name == "create_reminder" and isinstance(result, dict):
        return f"Przypomnienie: {result['text']} — {result['date']} {result['time']}."
    if name == "update_reminder" and isinstance(result, dict):
        if args.get("done"):
            return f"Odhaczylem: {result['text']}."
        return f"Zmienilem przypomnienie: {result['text']}."
    if name == "delete_reminder":
        return "Usunalem przypomnienie." if result.get("deleted") else "Nie znalazlem przypomnienia."
    if name == "create_note" and isinstance(result, dict):
        return f"Zapisalem notatke: {result['title'] or 'bez tytulu'}."
    if name == "remember_fact" and isinstance(result, dict):
        return f"Zapamietalem: {result['value']}."
    if name == "log_activity" and isinstance(result, dict):
        return f"Odnotowalem: {result['activity']} ({result['minutes']} min)."
    if name == "create_routine" and isinstance(result, dict):
        return f"Rutyna: {result['title']} o {result['default_start']}."
    return None


def _confirmation(executed):
    lines = [d for d in (_describe(n, a, r) for n, a, r in executed) if d]
    return "\n".join(lines) if lines else "Gotowe."


# Past-tense action verbs, with and without Polish diacritics since the model
# is told to answer without them but does not always comply.
_CLAIM_WORDS = (
    "dodalem", "dodałem", "zaplanowalem", "zaplanowałem", "zapisalem", "zapisałem",
    "ustawilem", "ustawiłem", "przesunalem", "przesunąłem", "usunalem", "usunąłem",
    "utworzylem", "utworzyłem", "zmienilem", "zmieniłem", "zarezerwowalem", "zarezerwowałem",
)


def _claims_action(text):
    low = (text or "").lower()
    return any(w in low for w in _CLAIM_WORDS)


# Sections of the injected context block. The model occasionally echoes the
# whole thing back instead of answering, which would dump the raw prompt into
# the chat window.
_LEAK_MARKERS = ("STAN NA TERAZ:", "PLAN NA DZIS", "PLAN NA JUTRO",
                 "CO WIEM O UZYTKOWNIKU", "RUTYNY:", "PREFERENCJE:", "ZASADY:")

# gemma4 sometimes emits a tool call as plain text rather than as a real
# tool_call, e.g.  remember_fact{key:<|"|>x<|"|>,value:<|"|>y<|"|>}
_LEAKED_CALL = re.compile(r"^\s*(\w+)\s*\{(.*)\}\s*$", re.S)


def _salvage_leaked_call(text):
    """Recover a tool call the model wrote as text instead of calling.

    Without this the user's instruction is silently dropped and they see
    raw tool syntax in the chat. Returns (name, args) or None.
    """
    m = _LEAKED_CALL.match(text or "")
    if not m or m.group(1) not in TOOL_REGISTRY:
        return None
    name, body = m.group(1), m.group(2).replace('<|"|>', '"')
    args = {}
    for part in re.findall(r'(\w+)\s*:\s*"([^"]*)"', body):
        args[part[0]] = part[1]
    return (name, args) if args else None


def _clean_reply(text):
    """Trim any echoed prompt scaffolding off the end of a reply."""
    text = (text or "").strip()
    for marker in _LEAK_MARKERS:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].strip()
        elif idx == 0:
            return ""
    return text.strip()


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _call_model(messages):
    return ollama.chat(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        think=False,          # the single biggest latency win — see module docstring
        keep_alive=KEEP_ALIVE,
        options=OPTIONS,
    )


def _execute(tool_calls):
    """Runs every tool the model asked for in this turn. Returns
    (executed, results_for_model, touched_read_tool)."""
    executed, payloads, touched_read = [], [], False
    for tc in tool_calls:
        fn = tc["function"]
        name = fn["name"]
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name in READ_TOOLS:
            touched_read = True
        if name not in TOOL_REGISTRY:
            result = {"error": f"unknown tool: {name}"}
        else:
            try:
                result = TOOL_REGISTRY[name](**args)
            except Exception as exc:  # let the model see and recover from it
                result = {"error": str(exc)}
        executed.append((name, args, result))
        payloads.append({
            "role": "tool",
            "content": json.dumps(result, ensure_ascii=False, default=str)[:2000],
        })
    return executed, payloads, touched_read


def run_agent_stream(user_message):
    """Generator yielding {"type": "status"|"final"|"error", ...} frames.

    Status frames exist so the UI can show progress while the model generates,
    which is what makes a ~15s local turn feel responsive rather than frozen.
    """
    # Same-day history only. Older turns carry stale "jutro = <old date>"
    # phrasing that the model copies instead of using the injected date.
    history = [{"role": m["role"], "content": m["content"]}
               for m in db.load_recent_messages(8, same_day_only=True)]
    messages = ([{"role": "system", "content": build_system_prompt()}]
                + history + [{"role": "user", "content": user_message}])
    db.save_message("user", user_message)

    all_executed = []
    nudged = False

    for _ in range(MAX_STEPS):
        yield {"type": "status", "text": "Mysle…"}
        try:
            response = _call_model(messages)
        except Exception as exc:
            reply = f"Blad modelu: {exc}"
            db.save_message("assistant", reply)
            yield {"type": "error", "text": reply}
            return

        msg = response["message"]
        messages.append(msg)
        tool_calls = msg.model_dump().get("tool_calls")

        if not tool_calls:
            reply = _clean_reply(msg.model_dump().get("content"))

            # Sometimes the tool call arrives as plain text rather than as a
            # real tool_call. Run it rather than dropping the user's request.
            salvaged = _salvage_leaked_call(reply)
            if salvaged:
                name, args = salvaged
                try:
                    result = TOOL_REGISTRY[name](**args)
                except Exception as exc:
                    result = {"error": str(exc)}
                all_executed.append((name, args, result))
                reply = _confirmation([(name, args, result)])
                db.save_message("assistant", reply)
                yield {"type": "final", "text": reply, "changed": True}
                return

            # The model sometimes narrates an action it never performed
            # ("zaplanowalem Ci trening...") without calling a tool, which
            # silently loses the user's data. If nothing was actually
            # executed but the reply claims otherwise, the claim is false by
            # construction — push back once and let it do the work.
            if not all_executed and not nudged and _claims_action(reply):
                nudged = True
                messages.append({
                    "role": "user",
                    "content": "Nie wywolales zadnego narzedzia, wiec NIC nie zostalo zapisane. "
                               "Wywolaj teraz odpowiednie narzedzie, bez tlumaczen.",
                })
                continue

            if not reply and all_executed:
                reply = _confirmation(all_executed)
            db.save_message("assistant", reply or "Gotowe.")
            yield {"type": "final", "text": reply or "Gotowe.",
                   "changed": bool(all_executed)}
            return

        yield {"type": "status",
               "text": "Zapisuje…" if any(
                   tc["function"]["name"] not in READ_TOOLS for tc in tool_calls
               ) else "Sprawdzam kalendarz…"}

        executed, payloads, touched_read = _execute(tool_calls)
        all_executed.extend(executed)
        messages.extend(payloads)

        # Pure-mutation turn: we already know exactly what happened, so skip
        # the extra ~20s round trip just to have the model phrase it.
        if not touched_read:
            reply = _confirmation(executed)
            db.save_message("assistant", reply)
            yield {"type": "final", "text": reply, "changed": True}
            return

    reply = _confirmation(all_executed) if all_executed else \
        "Nie udalo mi sie tego dokonczyc — sprecyzuj polecenie."
    db.save_message("assistant", reply)
    yield {"type": "final", "text": reply, "changed": bool(all_executed)}


def run_agent(user_message):
    """Blocking wrapper for the non-streaming endpoint."""
    final = "Gotowe."
    changed = False
    for frame in run_agent_stream(user_message):
        if frame["type"] in ("final", "error"):
            final = frame["text"]
            changed = frame.get("changed", False)
    return {"reply": final, "changed": changed}


def warmup():
    """Prime the KV cache at startup with the exact stable prefix real
    requests use — the tool schemas plus STATIC_RULES.

    This is not just a model load. Prefilling those ~1300 tokens costs ~13s
    the first time and ~0s afterwards, so paying it here means the user's
    first message is a cache hit instead of the slowest request of the
    session. The tools= and the static prefix must match _call_model exactly
    or the cache will not be reused.
    """
    try:
        ollama.chat(
            model=MODEL,
            messages=[{"role": "system", "content": STATIC_RULES},
                      {"role": "user", "content": "ok"}],
            tools=TOOLS,
            think=False,
            keep_alive=KEEP_ALIVE,
            options={**OPTIONS, "num_predict": 1},
        )
        return True
    except Exception:
        return False
