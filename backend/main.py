"""
Kalendarz Agent — API server.

Two clearly separated paths, which is the core of the app feeling fast:

  * REST CRUD (/api/events, /api/reminders, /api/notes, /api/memory/*) —
    no model involved at all. Dragging an event, switching views, creating
    or editing anything from the UI is a plain SQLite write and returns in
    single-digit milliseconds.

  * /api/chat/stream — the agent loop, the only place that pays LLM latency.

The frontend is served from this same origin (see the StaticFiles mount at the
bottom), so there is no CORS hop and ES modules load without a build step.

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000
Then open http://localhost:8000
"""

import datetime
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import db

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Kalendarz Agent API")

# Still permissive so opening calendar.html straight from disk keeps working.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()


@app.on_event("startup")
def _startup():
    # Fire and forget — a failed warm-up just means the first chat is slower,
    # it must never stop the server (and the UI is useful without the model).
    import threading
    threading.Thread(target=agent.warmup, daemon=True).start()


# ---------------------------------------------------------------------------
# Events
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
def list_events(date: Optional[str] = None, start: Optional[str] = None,
                end: Optional[str] = None):
    return db.get_events(date=date, start_date=start, end_date=end)


@app.post("/api/events")
def create_event(payload: EventIn):
    return db.create_event(**payload.model_dump())


@app.put("/api/events/{event_id}")
def update_event(event_id: str, payload: EventUpdate):
    ev = db.update_event(event_id, **payload.model_dump())
    if not ev:
        raise HTTPException(404, "event not found")
    return ev


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    if not db.delete_event(event_id):
        raise HTTPException(404, "event not found")
    return {"deleted": True}


@app.get("/api/events/conflicts")
def conflicts(date: str, start: str, end: str, exclude: Optional[str] = None):
    return db.find_conflicts(date, start, end, exclude)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

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
    return db.get_reminders(date)


@app.post("/api/reminders")
def create_reminder(payload: ReminderIn):
    return db.create_reminder(**payload.model_dump())


@app.put("/api/reminders/{reminder_id}")
def update_reminder(reminder_id: str, payload: ReminderUpdate):
    r = db.update_reminder(reminder_id, **payload.model_dump())
    if not r:
        raise HTTPException(404, "reminder not found")
    return r


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder(reminder_id: str):
    if not db.delete_reminder(reminder_id):
        raise HTTPException(404, "reminder not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

class NoteIn(BaseModel):
    title: Optional[str] = ""
    content: Optional[str] = ""


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


@app.get("/api/notes")
def list_notes():
    return db.get_notes()


@app.post("/api/notes")
def create_note(payload: NoteIn):
    return db.create_note(**payload.model_dump())


@app.put("/api/notes/{note_id}")
def update_note(note_id: str, payload: NoteUpdate):
    n = db.update_note(note_id, **payload.model_dump())
    if not n:
        raise HTTPException(404, "note not found")
    return n


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    if not db.delete_note(note_id):
        raise HTTPException(404, "note not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Memory — facts, routines, activity history
# ---------------------------------------------------------------------------

class FactIn(BaseModel):
    key: str
    value: str
    category: Optional[str] = "other"


@app.get("/api/memory/facts")
def list_facts(category: Optional[str] = None):
    return db.get_facts(category)


@app.post("/api/memory/facts")
def add_fact(payload: FactIn):
    return db.remember_fact(payload.key, payload.value, payload.category)


@app.delete("/api/memory/facts/{key}")
def remove_fact(key: str):
    if not db.forget_fact(key):
        raise HTTPException(404, "fact not found")
    return {"deleted": True}


class RoutineIn(BaseModel):
    title: str
    category: Optional[str] = "habit"
    default_start: str
    duration_minutes: int
    weekdays: Optional[str] = "0,1,2,3,4,5,6"


@app.get("/api/memory/routines")
def list_routines():
    return db.get_routines()


@app.post("/api/memory/routines")
def add_routine(payload: RoutineIn):
    return db.create_routine(**payload.model_dump())


@app.delete("/api/memory/routines/{routine_id}")
def remove_routine(routine_id: str):
    if not db.delete_routine(routine_id):
        raise HTTPException(404, "routine not found")
    return {"deleted": True}


@app.get("/api/memory/activities")
def list_activities():
    return db.all_activities()


@app.get("/api/memory/activities/{activity}")
def activity_detail(activity: str):
    return db.activity_stats(activity)


class ActivityIn(BaseModel):
    activity: str
    date: str
    minutes: Optional[int] = 0


@app.post("/api/memory/activities")
def add_activity(payload: ActivityIn):
    return db.log_activity(**payload.model_dump())


# ---------------------------------------------------------------------------
# Preferences (kept for compatibility with the older key/value store)
# ---------------------------------------------------------------------------

class PreferenceIn(BaseModel):
    key: str
    value: str


@app.get("/api/preferences")
def get_preferences():
    return db.get_preferences()


@app.put("/api/preferences")
def set_preference(payload: PreferenceIn):
    return db.set_preference(payload.key, payload.value)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.get("/api/messages")
def get_messages(limit: int = 50):
    out = []
    for r in db.load_recent_messages(limit):
        try:
            t = datetime.datetime.fromisoformat(r["created_at"]).strftime("%H:%M")
        except (ValueError, TypeError):
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
        return agent.run_agent(payload.message)
    except Exception as exc:
        raise HTTPException(500, f"agent error: {exc}")


@app.post("/api/chat/stream")
def chat_stream(payload: ChatIn):
    """Server-sent events. Emits status frames while tools run so the UI can
    show progress during the ~15s the local model needs, then a final frame."""
    if not payload.message.strip():
        raise HTTPException(400, "empty message")

    def gen():
        try:
            for frame in agent.run_agent_stream(payload.message):
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        except Exception as exc:
            err = {"type": "error", "text": f"Blad: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/health")
def health():
    return {"status": "ok", "model": agent.MODEL}


# ---------------------------------------------------------------------------
# Frontend — mounted last so it never shadows an /api route
# ---------------------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
