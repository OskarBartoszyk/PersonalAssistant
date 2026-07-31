"""
Data layer — SQLite schema and all data access.

Shared by the REST endpoints (fast, no LLM) and by the agent's tools.
Every table is created with IF NOT EXISTS and every migration is additive,
so an existing kalendarz.db keeps its data.

Single-user by design. Tables carry no user_id; adding one later is a
straightforward ALTER TABLE + WHERE-clause change, which is why all access
goes through the helpers here rather than inline SQL elsewhere.
"""

import datetime
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = str(Path(__file__).parent / "kalendarz.db")
TZ = ZoneInfo("Europe/Warsaw")

VALID_CATEGORIES = {"habit", "entertainment", "meeting", "important", "focus"}

# Categories for remembered facts. Kept loose on purpose — the agent picks
# one, and an unknown value degrades to "other" rather than being rejected.
FACT_CATEGORIES = {
    "hobby",       # what the user does for enjoyment
    "preference",  # standing preferences ("workouts are always 40 min")
    "routine",     # recurring rhythms ("gym on mon/wed/fri")
    "person",      # people in the user's life
    "goal",        # what they're working towards
    "health",      # sleep, diet, injuries
    "work",        # job, studies, projects
    "other",
}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets the UI read while the agent writes, instead of hitting
    # "database is locked" during a long tool call.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso():
    return datetime.datetime.now(TZ).isoformat()


def today_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def new_id():
    return str(uuid.uuid4())[:8]


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

        # --- memory: durable facts the assistant learns about the user ---
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_facts(
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'other',
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            source TEXT DEFAULT 'user_stated',
            created_at TEXT,
            updated_at TEXT,
            use_count INTEGER DEFAULT 0
        )""")

        # --- routines: recurring blocks the assistant can lay down itself ---
        conn.execute("""CREATE TABLE IF NOT EXISTS routines(
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'habit',
            default_start TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
            active INTEGER DEFAULT 1,
            created_at TEXT
        )""")

        # --- activity log: "how long have I been doing this" ---
        conn.execute("""CREATE TABLE IF NOT EXISTS activity_log(
            id TEXT PRIMARY KEY,
            activity TEXT NOT NULL,
            date TEXT NOT NULL,
            minutes INTEGER DEFAULT 0,
            event_id TEXT,
            created_at TEXT
        )""")

        # Indexes matter here: the week and month views pull date ranges on
        # every navigation, and that has to stay instant.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_name ON activity_log(activity, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_cat ON memory_facts(category)")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def get_events(date=None, start_date=None, end_date=None):
    with db() as conn:
        if date:
            rows = conn.execute(
                "SELECT * FROM events WHERE date=? ORDER BY start", (date,)
            ).fetchall()
        elif start_date and end_date:
            rows = conn.execute(
                "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date, start",
                (start_date, end_date),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY date, start").fetchall()
        return [dict(r) for r in rows]


def get_event(eid):
    with db() as conn:
        r = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return dict(r) if r else None


def create_event(title, category, date, start, end, description=""):
    if category not in VALID_CATEGORIES:
        category = "important"
    eid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO events(id,title,category,date,start,end,description) "
            "VALUES(?,?,?,?,?,?,?)",
            (eid, title, category, date, start, end, description or ""),
        )
    return get_event(eid)


def update_event(eid, **fields):
    if not get_event(eid):
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
    return get_event(eid)


def delete_event(eid):
    with db() as conn:
        return conn.execute("DELETE FROM events WHERE id=?", (eid,)).rowcount > 0


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(mins):
    mins = max(0, min(24 * 60, int(mins)))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def find_free_slots(date, duration_minutes, work_start="08:00", work_end="22:00"):
    events = sorted(get_events(date=date), key=lambda e: e["start"])
    cursor = to_min(work_start)
    end_of_day = to_min(work_end)
    free = []
    for e in events:
        s, en = to_min(e["start"]), to_min(e["end"])
        if s > cursor and s - cursor >= duration_minutes:
            free.append({"start": to_hhmm(cursor), "end": to_hhmm(s)})
        cursor = max(cursor, en)
    if end_of_day - cursor >= duration_minutes:
        free.append({"start": to_hhmm(cursor), "end": to_hhmm(end_of_day)})
    return free


def find_conflicts(date, start, end, exclude_id=None):
    """Events on `date` overlapping the [start, end) window."""
    s, e = to_min(start), to_min(end)
    out = []
    for ev in get_events(date=date):
        if exclude_id and ev["id"] == exclude_id:
            continue
        if to_min(ev["start"]) < e and to_min(ev["end"]) > s:
            out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def get_reminders(date=None):
    with db() as conn:
        if date:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE date=? ORDER BY time", (date,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reminders ORDER BY date, time").fetchall()
        return [dict(r) for r in rows]


def get_reminder(rid):
    with db() as conn:
        r = conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def create_reminder(text, date, time):
    rid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO reminders(id,text,date,time,done) VALUES(?,?,?,?,0)",
            (rid, text, date, time),
        )
    return get_reminder(rid)


def update_reminder(rid, **fields):
    if not get_reminder(rid):
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
    return get_reminder(rid)


def delete_reminder(rid):
    with db() as conn:
        return conn.execute("DELETE FROM reminders WHERE id=?", (rid,)).rowcount > 0


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def get_notes():
    with db() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_note(nid):
    with db() as conn:
        r = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        return dict(r) if r else None


def create_note(title="", content=""):
    nid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO notes(id,title,content,updated_at) VALUES(?,?,?,?)",
            (nid, title or "", content or "", now_iso()),
        )
    return get_note(nid)


def update_note(nid, **fields):
    if not get_note(nid):
        return None
    cols, vals = [], []
    for k, v in fields.items():
        if v is None:
            continue
        cols.append(f"{k}=?")
        vals.append(v)
    cols.append("updated_at=?")
    vals.append(now_iso())
    vals.append(nid)
    with db() as conn:
        conn.execute(f"UPDATE notes SET {', '.join(cols)} WHERE id=?", vals)
    return get_note(nid)


def delete_note(nid):
    with db() as conn:
        return conn.execute("DELETE FROM notes WHERE id=?", (nid,)).rowcount > 0


# ---------------------------------------------------------------------------
# Preferences (simple key/value, kept for backwards compatibility)
# ---------------------------------------------------------------------------

def get_preferences():
    with db() as conn:
        rows = conn.execute("SELECT key,value FROM preferences").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_preference(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO preferences(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    return get_preferences()


# ---------------------------------------------------------------------------
# Memory facts
# ---------------------------------------------------------------------------

def get_facts(category=None):
    with db() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM memory_facts WHERE category=? ORDER BY updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_facts ORDER BY category, updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def remember_fact(key, value, category="other", source="user_stated"):
    """Upsert a fact by key, so re-stating something updates instead of duplicating."""
    if category not in FACT_CATEGORIES:
        category = "other"
    key = key.strip().lower().replace(" ", "_")
    with db() as conn:
        conn.execute(
            """INSERT INTO memory_facts(id,category,key,value,source,created_at,updated_at,use_count)
               VALUES(?,?,?,?,?,?,?,0)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value,
                 category=excluded.category,
                 updated_at=excluded.updated_at""",
            (new_id(), category, key, str(value), source, now_iso(), now_iso()),
        )
        r = conn.execute("SELECT * FROM memory_facts WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None


def forget_fact(key):
    with db() as conn:
        return conn.execute(
            "DELETE FROM memory_facts WHERE key=?", (key.strip().lower().replace(" ", "_"),)
        ).rowcount > 0


def search_facts(query):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_facts WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------

def get_routines(active_only=True):
    with db() as conn:
        sql = "SELECT * FROM routines"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY default_start"
        return [dict(r) for r in conn.execute(sql).fetchall()]


def create_routine(title, category, default_start, duration_minutes, weekdays="0,1,2,3,4,5,6"):
    if category not in VALID_CATEGORIES:
        category = "habit"
    rid = new_id()
    with db() as conn:
        conn.execute(
            "INSERT INTO routines(id,title,category,default_start,duration_minutes,weekdays,active,created_at) "
            "VALUES(?,?,?,?,?,?,1,?)",
            (rid, title, category, default_start, int(duration_minutes), weekdays, now_iso()),
        )
        r = conn.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def delete_routine(rid):
    with db() as conn:
        return conn.execute("DELETE FROM routines WHERE id=?", (rid,)).rowcount > 0


def routines_for_weekday(weekday):
    """weekday: 0=Monday .. 6=Sunday"""
    return [r for r in get_routines() if str(weekday) in r["weekdays"].split(",")]


# ---------------------------------------------------------------------------
# Activity log — powers "how long have I been doing X"
# ---------------------------------------------------------------------------

def log_activity(activity, date, minutes=0, event_id=None):
    activity = activity.strip().lower()
    with db() as conn:
        # One entry per activity per day, so re-logging corrects rather than doubles.
        existing = conn.execute(
            "SELECT id FROM activity_log WHERE activity=? AND date=?", (activity, date)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE activity_log SET minutes=?, event_id=? WHERE id=?",
                (int(minutes), event_id, existing["id"]),
            )
            aid = existing["id"]
        else:
            aid = new_id()
            conn.execute(
                "INSERT INTO activity_log(id,activity,date,minutes,event_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (aid, activity, date, int(minutes), event_id, now_iso()),
            )
        r = conn.execute("SELECT * FROM activity_log WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def activity_stats(activity):
    """How long the user has been doing something, how often, and their streak."""
    activity = activity.strip().lower()
    with db() as conn:
        rows = conn.execute(
            "SELECT date, minutes FROM activity_log WHERE activity=? ORDER BY date",
            (activity,),
        ).fetchall()

    if not rows:
        return {"activity": activity, "found": False}

    dates = [datetime.date.fromisoformat(r["date"]) for r in rows]
    total_minutes = sum(r["minutes"] or 0 for r in rows)
    first, last = dates[0], dates[-1]
    today = datetime.datetime.now(TZ).date()
    days_since_start = (today - first).days

    # Current streak counts consecutive days backwards from the most recent
    # entry, but only if that entry is today or yesterday — otherwise it broke.
    streak = 0
    if (today - last).days <= 1:
        streak = 1
        for i in range(len(dates) - 1, 0, -1):
            if (dates[i] - dates[i - 1]).days == 1:
                streak += 1
            else:
                break

    longest, run = 1, 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    weeks = max(days_since_start / 7.0, 1.0)
    return {
        "activity": activity,
        "found": True,
        "first_date": first.isoformat(),
        "last_date": last.isoformat(),
        "days_since_start": days_since_start,
        "months_since_start": round(days_since_start / 30.44, 1),
        "total_sessions": len(rows),
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60.0, 1),
        "avg_minutes_per_session": round(total_minutes / len(rows)) if rows else 0,
        "sessions_per_week": round(len(rows) / weeks, 1),
        "current_streak_days": streak,
        "longest_streak_days": longest,
    }


def all_activities():
    with db() as conn:
        rows = conn.execute(
            """SELECT activity, COUNT(*) AS sessions, SUM(minutes) AS minutes,
                      MIN(date) AS first_date, MAX(date) AS last_date
               FROM activity_log GROUP BY activity ORDER BY sessions DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

def save_message(role, content):
    with db() as conn:
        conn.execute(
            "INSERT INTO messages(id,role,content,created_at) VALUES(?,?,?,?)",
            (new_id(), role, content, now_iso()),
        )


def load_recent_messages(limit=20, same_day_only=False):
    """`same_day_only` scopes history to today.

    This matters more than it looks: yesterday's conversation is full of
    phrases like "jutro, czwartek 2026-07-30", and a small local model will
    happily copy that date into today's answer rather than re-deriving it.
    Telling the model to ignore stale dates in the prompt does not reliably
    work; not showing them to it does.
    """
    with db() as conn:
        if same_day_only:
            rows = conn.execute(
                "SELECT role,content,created_at FROM messages "
                "WHERE date(created_at)=date(?) ORDER BY created_at DESC LIMIT ?",
                (now_iso(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role,content,created_at FROM messages "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
