"""
Kalendarz Agent — Backend

FastAPI + SQLite + Ollama tool-calling agent, built on top of the original
minimal agent loop. Exposes:

  * plain REST endpoints (/api/events, /api/reminders, /api/notes,
    /api/preferences) used by the frontend for fast, direct CRUD — no LLM
    involved when the user clicks "+" in the UI.

  * a /api/chat endpoint that runs the agent loop: the LLM reads the
    message, decides which tool(s) to call, Python executes them against
    the same SQLite database, and the loop continues until the model
    produces a final natural-language reply.

Run:
    pip install -r requirements.txt
    ollama pull <a tool-calling capable model, e.g. qwen2.5:7b or llama3.1:8b>
    uvicorn main:app --reload --port 8000

The frontend (kalendarz_agent.html) expects this server on
http://localhost:8000 — see API_BASE in the HTML file.
"""

import datetime
import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Optional
from zoneinfo import ZoneInfo

import ollama
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = "kalendarz.db"

# Change this to whatever tool-calling capable tag you have pulled locally.
# (Ollama tool-calling requires a model that supports it — check `ollama show <model>`.)
MODEL = "gemma4:e4b"

VALID_CATEGORIES = {"habit", "entertainment", "meeting", "important", "focus"}

app = FastAPI(title="Kalendarz Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS events(
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            description TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS reminders(
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            done INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notes(
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS preferences(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT
        )""")


init_db()


# ---------------------------------------------------------------------------
# Data access helpers (shared by REST endpoints and by the LLM tools)
# ---------------------------------------------------------------------------

def _new_id():
    return str(uuid.uuid4())[:8]


def _now_iso():
    return datetime.datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()


# --- events ---

def db_get_events(date=None, start_date=None, end_date=None):
    with db() as conn:
        if date:
            rows = conn.execute("SELECT * FROM events WHERE date=? ORDER BY start", (date,)).fetchall()
        elif start_date and end_date:
            rows = conn.execute(
                "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date, start",
                (start_date, end_date),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY date, start").fetchall()
        return [dict(r) for r in rows]


def db_get_event(eid):
    with db() as conn:
        r = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return dict(r) if r else None


def db_create_event(title, category, date, start, end, description=""):
    if category not in VALID_CATEGORIES:
        category = "important"
    eid = _new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO events(id,title,category,date,start,end,description) VALUES(?,?,?,?,?,?,?)",
            (eid, title, category, date, start, end, description or ""),
        )
    return db_get_event(eid)


def db_update_event(eid, **fields):
    if not db_get_event(eid):
        return None
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k == "category" and v not in VALID_CATEGORIES:
            continue
        cols.append(f"{k}=?")
        vals.append(v)
    if cols:
        vals.append(eid)
        with db() as conn:
            conn.execute(f"UPDATE events SET {', '.join(cols)} WHERE id=?", vals)
    return db_get_event(eid)


def db_delete_event(eid):
    with db() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=?", (eid,))
        return cur.rowcount > 0


def _to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(mins):
    return f"{mins // 60:02d}:{mins % 60:02d}"


def db_find_free_slots(date, duration_minutes, work_start="08:00", work_end="22:00"):
    events = sorted(db_get_events(date=date), key=lambda e: e["start"])
    cursor = _to_min(work_start)
    end_of_day = _to_min(work_end)
    free = []
    for e in events:
        s, en = _to_min(e["start"]), _to_min(e["end"])
        if s > cursor and s - cursor >= duration_minutes:
            free.append({"start": _to_hhmm(cursor), "end": _to_hhmm(s)})
        cursor = max(cursor, en)
    if end_of_day - cursor >= duration_minutes:
        free.append({"start": _to_hhmm(cursor), "end": _to_hhmm(end_of_day)})
    return free


# --- reminders ---

def db_get_reminders(date=None):
    with db() as conn:
        if date:
            rows = conn.execute("SELECT * FROM reminders WHERE date=? ORDER BY time", (date,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reminders ORDER BY date, time").fetchall()
        return [dict(r) for r in rows]


def db_get_reminder(rid):
    with db() as conn:
        r = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def db_create_reminder(text, date, time):
    rid = _new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO reminders(id,text,date,time,done) VALUES(?,?,?,?,0)",
            (rid, text, date, time),
        )
    return db_get_reminder(rid)


def db_update_reminder(rid, **fields):
    if not db_get_reminder(rid):
        return None
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        if k == "done":
            v = 1 if v else 0
        cols.append(f"{k}=?")
        vals.append(v)
    if cols:
        vals.append(rid)
        with db() as conn:
            conn.execute(f"UPDATE reminders SET {', '.join(cols)} WHERE id=?", vals)
    return db_get_reminder(rid)


def db_delete_reminder(rid):
    with db() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
        return cur.rowcount > 0


# --- notes ---

def db_get_notes():
    with db() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def db_get_note(nid):
    with db() as conn:
        r = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        return dict(r) if r else None


def db_create_note(title="", content=""):
    nid = _new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO notes(id,title,content,updated_at) VALUES(?,?,?,?)",
            (nid, title or "", content or "", _now_iso()),
        )
    return db_get_note(nid)


def db_update_note(nid, **fields):
    if not db_get_note(nid):
        return None
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(_now_iso())
    vals.append(nid)
    with db() as conn:
        conn.execute(f"UPDATE notes SET {', '.join(cols)} WHERE id=?", vals)
    return db_get_note(nid)


def db_delete_note(nid):
    with db() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id=?", (nid,))
        return cur.rowcount > 0


# --- preferences ---

def db_get_user_preferences():
    with db() as conn:
        rows = conn.execute("SELECT key,value FROM preferences").fetchall()
        return {r["key"]: r["value"] for r in rows}


def db_update_user_preferences(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO preferences(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    return db_get_user_preferences()


# --- chat messages (for continuity + frontend history) ---

def save_message(role, content):
    with db() as conn:
        conn.execute(
            "INSERT INTO messages(id,role,content,created_at) VALUES(?,?,?,?)",
            (_new_id(), role, content, _now_iso()),
        )


def load_recent_messages(limit=20):
    with db() as conn:
        rows = conn.execute(
            "SELECT role,content,created_at FROM messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Tools exposed to the LLM
# ---------------------------------------------------------------------------

def get_current_time():
    """Returns the current date, time and weekday in Poland."""
    now = datetime.datetime.now(ZoneInfo("Europe/Warsaw"))
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
    }


def get_calendar_events(date: str = None, start_date: str = None, end_date: str = None):
    """Returns calendar events. Pass `date` for a single day, or
    `start_date`/`end_date` for a range (YYYY-MM-DD)."""
    return db_get_events(date=date, start_date=start_date, end_date=end_date)


def create_calendar_event(title: str, category: str, date: str, start: str, end: str, description: str = ""):
    """Creates a calendar event. category must be one of:
    habit, entertainment, meeting, important, focus. date=YYYY-MM-DD, start/end=HH:MM."""
    return db_create_event(title, category, date, start, end, description)


def update_calendar_event(id: str, title: str = None, category: str = None, date: str = None,
                           start: str = None, end: str = None, description: str = None):
    """Updates an existing calendar event by id. Only pass the fields that change."""
    result = db_update_event(id, title=title, category=category, date=date, start=start,
                              end=end, description=description)
    return result or {"error": f"no event with id {id}"}


def delete_calendar_event(id: str):
    """Deletes a calendar event by id."""
    return {"deleted": db_delete_event(id)}


def find_free_slots(date: str, duration_minutes: int, work_start: str = "08:00", work_end: str = "22:00"):
    """Finds free time windows of at least `duration_minutes` on `date`,
    within the work_start–work_end bounds (defaults 08:00–22:00)."""
    return db_find_free_slots(date, duration_minutes, work_start, work_end)


def get_reminders(date: str = None):
    """Returns reminders, optionally filtered to a single date (YYYY-MM-DD)."""
    return db_get_reminders(date)


def create_reminder(text: str, date: str, time: str):
    """Creates a reminder. date=YYYY-MM-DD, time=HH:MM."""
    return db_create_reminder(text, date, time)


def update_reminder(id: str, text: str = None, date: str = None, time: str = None, done: bool = None):
    """Updates an existing reminder by id. Only pass the fields that change."""
    result = db_update_reminder(id, text=text, date=date, time=time, done=done)
    return result or {"error": f"no reminder with id {id}"}


def delete_reminder(id: str):
    """Deletes a reminder by id."""
    return {"deleted": db_delete_reminder(id)}


def get_notes():
    """Returns all notes."""
    return db_get_notes()


def create_note(title: str = "", content: str = ""):
    """Creates a note."""
    return db_create_note(title, content)


def update_note(id: str, title: str = None, content: str = None):
    """Updates an existing note by id."""
    result = db_update_note(id, title=title, content=content)
    return result or {"error": f"no note with id {id}"}


def delete_note(id: str):
    """Deletes a note by id."""
    return {"deleted": db_delete_note(id)}


def get_user_preferences():
    """Returns learned user preferences as a key/value map
    (e.g. workout_duration_minutes, preferred_work_hours, sleep_hours)."""
    return db_get_user_preferences()


def update_user_preferences(key: str, value: str):
    """Stores or updates a single learned user preference. Use this whenever
    the user corrects the agent or states a standing preference
    (e.g. 'trening zawsze trwa 40 minut' -> key='workout_duration_minutes', value='40')."""
    return db_update_user_preferences(key, value)


tool_registry = {
    "get_current_time": get_current_time,
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
    "update_note": update_note,
    "delete_note": delete_note,
    "get_user_preferences": get_user_preferences,
    "update_user_preferences": update_user_preferences,
}

tools = [
    {"type": "function", "function": {
        "name": "get_current_time",
        "description": "Returns the current date, time and day of the week in Poland.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_calendar_events",
        "description": "Returns calendar events for a single date or a date range.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, for a single day"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD, range start"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD, range end"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_calendar_event",
        "description": "Creates a new calendar event.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "category": {"type": "string", "enum": list(VALID_CATEGORIES)},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "start": {"type": "string", "description": "HH:MM"},
            "end": {"type": "string", "description": "HH:MM"},
            "description": {"type": "string"},
        }, "required": ["title", "category", "date", "start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "update_calendar_event",
        "description": "Updates fields of an existing calendar event.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "category": {"type": "string", "enum": list(VALID_CATEGORIES)},
            "date": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_calendar_event",
        "description": "Deletes a calendar event by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "find_free_slots",
        "description": "Finds free time windows on a given date, at least duration_minutes long.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "duration_minutes": {"type": "integer"},
            "work_start": {"type": "string", "description": "HH:MM, default 08:00"},
            "work_end": {"type": "string", "description": "HH:MM, default 22:00"},
        }, "required": ["date", "duration_minutes"]},
    }},
    {"type": "function", "function": {
        "name": "get_reminders",
        "description": "Returns reminders, optionally filtered to one date.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_reminder",
        "description": "Creates a reminder.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "HH:MM"},
        }, "required": ["text", "date", "time"]},
    }},
    {"type": "function", "function": {
        "name": "update_reminder",
        "description": "Updates an existing reminder by id.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "date": {"type": "string"},
            "time": {"type": "string"},
            "done": {"type": "boolean"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_reminder",
        "description": "Deletes a reminder by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "get_notes",
        "description": "Returns all notes.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "create_note",
        "description": "Creates a note.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "update_note",
        "description": "Updates an existing note by id.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_note",
        "description": "Deletes a note by id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "get_user_preferences",
        "description": "Returns learned user preferences as a key/value map.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "update_user_preferences",
        "description": "Stores or updates one learned user preference (key/value).",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
        }, "required": ["key", "value"]},
    }},
]

SYSTEM_PROMPT_TEMPLATE = """
Jestes osobistym asystentem zarzadzajacym kalendarzem, przypomnieniami i notatkami uzytkownika.

{now_block}

ZASADA NADRZEDNA: Nigdy nie pisz, ze cos zostalo zaplanowane, dodane lub zapisane, jesli nie zrobiles
tego narzedziem w tej samej turze. Kiedy uzytkownik prosi o zaplanowanie czegos, od razu wykonaj
potrzebne wywolania create_calendar_event / create_reminder / update_calendar_event dla KAZDEGO
elementu planu - jedno po drugim - a dopiero potem opisz gotowy harmonogram jako fakt dokonany. Nie
pytaj o zgode na plan, ktory jestes w stanie ulozyc z podanych informacji - po prostu go zapisz. Pytaj
o potwierdzenie tylko wtedy, gdy naprawde nie jestes pewien, co zrobic (patrz sekcja PYTANIA nizej).

Zawsze korzystaj z dostarczonych narzedzi do odczytu i zmiany danych - nigdy nie zgaduj dat, godzin
ani tresci istniejacych wydarzen, przypomnien czy notatek.
Daty podawaj w formacie YYYY-MM-DD, godziny w formacie HH:MM (24h).
Dostepne kategorie wydarzen: habit, entertainment, meeting, important, focus.
Zanim zaplanujesz cos nowego, sprawdz istniejace wydarzenia narzedziem get_calendar_events,
aby uniknac konfliktow w czasie - jesli konflikt wystapi, zaproponuj wolny termin (find_free_slots).
Jesli uzytkownik poprawia Twoja decyzje lub podaje stala preferencje (np. "trening zawsze trwa 40 minut"),
zapisz ja narzedziem update_user_preferences i wykorzystuj przy kolejnym planowaniu - sprawdzaj
get_user_preferences zanim zaplanujesz nawyki takie jak trening, praca czy sen.

PYTANIA: zadawaj ich jak najmniej. Zbierz wszystkie brakujace informacje w JEDNYM pytaniu zamiast w
kilku kolejnych turach. Jesli czegos nie podano, a da sie przyjac rozsadne zalozenie domyslne (np.
nieznany czas trwania wizyty u lekarza = 1 godzina, "teraz" = aktualna godzina podana wyzej), przyjmij
je i napisz w odpowiedzi, jakie zalozenie przyjales, zamiast dopytywac. Pytaj tylko wtedy, gdy
brakujaca informacja naprawde uniemozliwia wykonanie akcji (np. nie wiadomo, ktorego dnia cos ma sie
odbyc).

Odpowiadaj krotko, po polsku, bez markdown i bez emoji.
"""


def build_system_prompt() -> str:
    """Injects the real current date/time as a fact, instead of relying on
    the model to decide to call get_current_time (small local models tend
    to anchor on dates already mentioned earlier in the chat history and
    skip re-checking, even when explicitly told to)."""
    now = datetime.datetime.now(ZoneInfo("Europe/Warsaw"))
    now_block = (
        f"Dzisiaj jest {now.strftime('%A')}, {now.strftime('%Y-%m-%d')}, "
        f"aktualna godzina to {now.strftime('%H:%M')} (Europe/Warsaw). To jest zawsze aktualna, "
        f"prawdziwa wartosc - ignoruj wszelkie inne daty/godziny wspomniane wczesniej w historii "
        f"tej rozmowy, mogly pochodzic z poprzedniego dnia."
    )
    return SYSTEM_PROMPT_TEMPLATE.format(now_block=now_block)


def run_agent(user_message: str) -> str:
    history = [{"role": m["role"], "content": m["content"]} for m in load_recent_messages(20)]
    messages = [{"role": "system", "content": build_system_prompt()}] + history + [
        {"role": "user", "content": user_message}
    ]
    save_message("user", user_message)

    max_steps = 8
    for _ in range(max_steps):
        response = ollama.chat(model=MODEL, messages=messages, tools=tools)
        assistant_message = response["message"]
        messages.append(assistant_message)

        tool_calls = assistant_message.get("tool_calls")
        if not tool_calls:
            reply = assistant_message.get("content", "")
            save_message("assistant", reply)
            return reply

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            arguments = tool_call["function"].get("arguments", {}) or {}
            if name not in tool_registry:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = tool_registry[name](**arguments)
                except Exception as exc:  # keep the loop alive, let the model see the error
                    result = {"error": str(exc)}
            messages.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False, default=str)})

    reply = "Nie udalo mi sie dokonczyc tego w rozsadnej liczbie krokow — sprobuj sprecyzowac polecenie."
    save_message("assistant", reply)
    return reply


# ---------------------------------------------------------------------------
# REST API (direct CRUD, used by the frontend UI — no LLM involved)
# ---------------------------------------------------------------------------

class EventIn(BaseModel):
    title: str
    category: str
    date: str
    start: str
    end: str
    description: Optional[str] = ""


class EventUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    date: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    description: Optional[str] = None


@app.get("/api/events")
def list_events(date: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None):
    return db_get_events(date=date, start_date=start, end_date=end)


@app.post("/api/events")
def create_event(payload: EventIn):
    return db_create_event(**payload.dict())


@app.put("/api/events/{event_id}")
def update_event(event_id: str, payload: EventUpdate):
    ev = db_update_event(event_id, **payload.dict())
    if not ev:
        raise HTTPException(404, "event not found")
    return ev


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    if not db_delete_event(event_id):
        raise HTTPException(404, "event not found")
    return {"deleted": True}


class ReminderIn(BaseModel):
    text: str
    date: str
    time: str


class ReminderUpdate(BaseModel):
    text: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    done: Optional[bool] = None


@app.get("/api/reminders")
def list_reminders(date: Optional[str] = None):
    return db_get_reminders(date)


@app.post("/api/reminders")
def create_reminder_endpoint(payload: ReminderIn):
    return db_create_reminder(**payload.dict())


@app.put("/api/reminders/{reminder_id}")
def update_reminder_endpoint(reminder_id: str, payload: ReminderUpdate):
    r = db_update_reminder(reminder_id, **payload.dict())
    if not r:
        raise HTTPException(404, "reminder not found")
    return r


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder_endpoint(reminder_id: str):
    if not db_delete_reminder(reminder_id):
        raise HTTPException(404, "reminder not found")
    return {"deleted": True}


class NoteIn(BaseModel):
    title: Optional[str] = ""
    content: Optional[str] = ""


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


@app.get("/api/notes")
def list_notes():
    return db_get_notes()


@app.post("/api/notes")
def create_note_endpoint(payload: NoteIn):
    return db_create_note(**payload.dict())


@app.put("/api/notes/{note_id}")
def update_note_endpoint(note_id: str, payload: NoteUpdate):
    n = db_update_note(note_id, **payload.dict())
    if not n:
        raise HTTPException(404, "note not found")
    return n


@app.delete("/api/notes/{note_id}")
def delete_note_endpoint(note_id: str):
    if not db_delete_note(note_id):
        raise HTTPException(404, "note not found")
    return {"deleted": True}


class PreferenceIn(BaseModel):
    key: str
    value: str


@app.get("/api/preferences")
def get_preferences():
    return db_get_user_preferences()


@app.put("/api/preferences")
def set_preference(payload: PreferenceIn):
    return db_update_user_preferences(payload.key, payload.value)


@app.get("/api/messages")
def get_messages(limit: int = 50):
    rows = load_recent_messages(limit)
    out = []
    for r in rows:
        try:
            t = datetime.datetime.fromisoformat(r["created_at"]).strftime("%H:%M")
        except Exception:
            t = ""
        out.append({"role": r["role"], "text": r["content"], "time": t})
    return out


class ChatIn(BaseModel):
    message: str


@app.post("/api/chat")
def chat(payload: ChatIn):
    if not payload.message.strip():
        raise HTTPException(400, "empty message")
    try:
        reply = run_agent(payload.message)
    except Exception as exc:
        raise HTTPException(500, f"agent error: {exc}")
    return {"reply": reply}


@app.get("/api/health")
def health():
    return {"status": "ok"}