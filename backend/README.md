# Kalendarz Agent

Osobisty asystent kalendarza: lokalny model (Ollama), FastAPI + SQLite,
frontend bez build-stepu.

## Uruchomienie

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Otwórz **http://localhost:8000** — backend serwuje też frontend, więc nie ma
CORS-a i moduły ES ładują się bez bundlera.

## Struktura

```
backend/
  main.py     REST API + SSE, montuje frontend
  db.py       schemat i dostęp do danych (SQLite)
  agent.py    pętla agenta Ollama
frontend/
  index.html
  css/app.css
  js/api.js       stan, cache wydarzeń, klient HTTP
  js/calendar.js  widoki dzień/tydzień/miesiąc/rok + drag & drop
  js/panels.js    przypomnienia, notatki, pamięć, modal
  js/chat.js      czat SSE + głos (Web Speech API)
  js/app.js       bootstrap
```

`calendar.html` w katalogu głównym to poprzednia, jednoplikowa wersja —
zastąpiona przez `frontend/`. Można ją usunąć.

## Wydajność

Profilowanie `gemma4:e4b` pokazało, gdzie faktycznie szedł czas:

| co | pomiar |
|---|---|
| przetwarzanie promptu | 150–225 tok/s |
| generowanie | 12,5 tok/s ← wąskie gardło |
| tokeny na jedno wywołanie narzędzia | ~357, z czego ~350 to ukryte rozumowanie |

Cztery zmiany skróciły żądanie z 90 s+ do ~10–20 s:

1. **`think=False`** — `gemma4:e4b` to model rozumujący. Emitował ~1200 znaków
   myślenia i zero treści, żeby wybrać jedno narzędzie. Wyłączenie: 514 → 106
   tokenów wyjścia, i model od razu wywołuje właściwe narzędzie zamiast
   marnować turę na odczyt.
2. **Wstrzykiwanie kontekstu** — plan na dziś i jutro, pamięć i preferencje
   trafiają prosto do promptu, więc model nie traci 30 s na `get_calendar_events`.
3. **Potwierdzenia generowane w Pythonie** — po samych zapisach odpowiedź
   („Dodalem: …”) buduje Python z tego, co naprawdę zrobiła baza. Oszczędza
   turę modelu i uniemożliwia modelowi twierdzenie, że coś zapisał, gdy nie zapisał.
4. **Stabilny prefiks promptu** — Ollama cache'uje KV prefiksu. Reguły statyczne
   i schematy narzędzi (~1300 tokenów) są na początku i nigdy się nie zmieniają,
   zmienny kontekst jest na końcu. Trafienie w cache: prefill 12,8 s → 0,0 s.
   `warmup()` przy starcie rozgrzewa dokładnie ten prefiks, więc pierwsza
   wiadomość użytkownika też jest szybka.

**Interfejs nie płaci tej latencji.** Przeciąganie, zmiana widoku, tworzenie
i edycja idą bezpośrednio do SQLite — model nie bierze w tym udziału.

## Pamięć

- `memory_facts` — trwałe fakty (hobby, preferencje, cele, osoby); upsert po kluczu.
- `routines` — cykliczne bloki tygodniowe.
- `activity_log` — historia aktywności; `activity_stats()` liczy staż, sumy,
  serie dni. Wydarzenia kategorii `habit` trafiają tu automatycznie.

Historia czatu podawana modelowi jest **ograniczona do bieżącego dnia** —
wczorajsze „jutro, czwartek 2026-07-30" powodowało, że model kopiował starą datę
zamiast wyliczyć nową.

## Skróty klawiszowe

`D` dzień · `W` tydzień · `M` miesiąc · `R` rok · `←/→` nawigacja · `T` dziś ·
`Spacja` mikrofon

## Głos

Web Speech API, lokalnie w przeglądarce (wymaga Chrome). Mówisz → asystent
odpowiada **głosem**. Piszesz → odpowiada **tekstem**.

## Narzędzia agenta

`create/update/delete_calendar_event`, `get_calendar_events`, `find_free_slots`,
`create/update/delete_reminder`, `get_reminders`, `create_note`, `get_notes`,
`remember_fact`, `recall_facts`, `get_activity_stats`, `log_activity`,
`create_routine`, `get_routines`.

## Model

`MODEL` w `agent.py`. Musi wspierać tool-calling — `gemma3:4b` **nie wspiera**,
`gemma4:12b-mlx` działa, ale jest wolniejszy (5 tok/s vs 12,5).
