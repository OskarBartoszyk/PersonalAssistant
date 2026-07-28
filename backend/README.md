# Kalendarz Agent — backend

## Uruchomienie

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

ollama pull <model>              # patrz uwaga niżej
uvicorn main:app --reload --port 8000
```

Serwer wystartuje na `http://localhost:8000`. Baza danych `kalendarz.db`
(SQLite) tworzy się automatycznie w tym samym katalogu przy pierwszym
uruchomieniu.

Otwórz `kalendarz_agent.html` w przeglądarce — front łączy się z
`http://localhost:8000` (zmienna `API_BASE` na górze skryptu).

## Uwaga o modelu

W `main.py` `MODEL = "gemma4:e4b"` — podmień na tag modelu, który faktycznie
masz ściągniętego w Ollama i który obsługuje tool-calling (np. `qwen2.5:7b`,
`llama3.1:8b`, `mistral-nemo`). Nie każdy model w Ollama wspiera `tools=`;
jeśli agent nigdy nie wywołuje narzędzi, to zwykle znaczy, że wybrany model
tego nie obsługuje.

## Co robi backend

- **REST CRUD** (`/api/events`, `/api/reminders`, `/api/notes`,
  `/api/preferences`) — używane bezpośrednio przez UI (przyciski „+”,
  edycja, usuwanie). Bez udziału modelu, więc jest natychmiastowe.
- **`POST /api/chat`** — wiadomość użytkownika trafia do pętli agenta:
  model decyduje, których narzędzi użyć (odczyt/zapis wydarzeń,
  przypomnień, notatek, wyszukiwanie wolnych okien, zapamiętywanie
  preferencji), Python je wykonuje na tej samej bazie SQLite, wynik wraca
  do modelu, aż wygeneruje ostateczną odpowiedź tekstową.
- **`GET /api/messages`** — historia czatu (zapisywana po stronie
  serwera), ładowana przez front przy starcie.

## Zestaw narzędzi agenta

`get_current_time`, `get_calendar_events`, `create_calendar_event`,
`update_calendar_event`, `delete_calendar_event`, `find_free_slots`,
`get_reminders`, `create_reminder`, `update_reminder`, `delete_reminder`,
`get_notes`, `create_note`, `update_note`, `delete_note`,
`get_user_preferences`, `update_user_preferences`.

`update_user_preferences` to mechanizm uczenia preferencji z Twojego
briefu projektu: gdy użytkownik poprawia agenta ("trening zawsze trwa
40 minut"), model zapisuje to jako parę klucz/wartość, a `SYSTEM_PROMPT`
instruuje go, żeby sprawdzał `get_user_preferences` przed planowaniem
nawyków.

## Dalsza rozbudowa (zgodnie z Twoim planem projektu)

- Etap 8 z Twojego dokumentu (pamięć semantyczna / RAG) łatwo dopiąć jako
  kolejną tabelę + nowe narzędzia `remember_fact` / `recall_facts`, bez
  zmiany reszty architektury.
- Speech-to-Text/Text-to-Speech (Etap 7) podłącza się jako osobny
  endpoint, np. `POST /api/voice` przyjmujący audio i zwracający tekst,
  wpięty przed `run_agent`.