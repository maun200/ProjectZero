# Contributing

*(English summary — full guide below is in German.)* Dev setup: `python -m venv .venv` then
`pip install -e '.[test]'`. Run tests with `pytest -q` (the `slow` marker covers multi-process
tests over ~20s; skip them locally with `-m "not slow"`). Workers never execute remote commands,
tests never download a real model, and nothing with secrets, databases or model weights gets
committed — see the checklist at the end before opening a PR.

---

## Mitwirken an ProjectZero

Danke für dein Interesse! ProjectZero ist ein experimentelles, community-getragenes Projekt
(Stufe: v0.3, Proof-of-Concept) — kein Produkt mit SLA. Bevor du anfängst, lies kurz `README.md`
und `docs/SECURITY.md`, damit du die aktuellen Grenzen kennst (z. B.: Prompts liegen im
Klartext in der Coordinator-DB, es gibt noch keine kryptographische Ergebnisverifikation).

### Dev-Setup

```bash
git clone <this-repo>
cd projectzero
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e '.[test]'
```

Python ≥ 3.11 wird vorausgesetzt (siehe `pyproject.toml`).

### Tests ausführen

```bash
pytest -q                 # alle schnellen Tests
pytest -q -m "not slow"   # explizit ohne die multi-process-Tests (> 20 s)
pytest -q tests/test_community.py   # nur eine Datei
```

Für einen manuellen Rauchtest ohne echtes Modell: `projectzero demo` startet Server und App
lokal mit einem Demo-Modell (kein Download nötig). `python scripts/installer_smoke.py projectzero`
spielt denselben Ablauf automatisch durch.

### Scope-Regeln (bitte unbedingt einhalten)

- **Worker führen niemals Remote-Befehle aus.** Ein Worker fragt den Coordinator nach einem Job
  (Prompt + Parameter) und schickt das Ergebnis zurück — er bekommt nie Code oder Shell-Befehle
  zur Ausführung zugeschickt. Jede Änderung, die das aufweicht, wird abgelehnt.
- **Tests laden kein echtes Modell herunter und starten keine echte Inferenz.** Nutze die
  vorhandenen Mocks/Fixtures (`tests/conftest.py`, `tests/grid_helpers.py`); ein Test, der ein
  GGUF/Ollama-Modell zieht, blockiert CI unnötig und ist nicht reproduzierbar.
- **Keine Secrets, Datenbanken oder Modell-Gewichte committen.** Kein `.env`, kein
  `*.sqlite3`/`*.db`, keine API-Keys/Tokens, keine `.gguf`/`.bin`-Gewichte, keine echten
  Logs mit Prompts oder Credentials. `.gitignore` deckt die üblichen Fälle ab — prüfe `git status`
  trotzdem vor jedem Commit.
- **Trusted/community-Trennung bleibt strikt.** `privacy=trusted` darf nie automatisch an einen
  `community`-Worker gehen, und ein `community`-Worker darf nie heimlich als `trusted`
  eingestuft werden. Wenn eine Änderung diese Grenze berührt, erst in `docs/SECURITY.md`
  dokumentieren, dann implementieren.
- Neue Endpunkte/Verhalten brauchen einen Test, der das Verhalten tatsächlich prüft (nicht nur
  den Happy Path) — siehe bestehende Tests unter `tests/` als Vorbild für Stil und Fixtures.

### Pull-Request-Checkliste

- [ ] `pytest -q` lokal grün (oder begründet, warum ein Test übersprungen wurde)
- [ ] `ruff check --select E9,F63,F7,F82 .` liefert keine neuen Fehler
- [ ] Keine Secrets/DBs/Gewichte/echten Prompts im Diff (`git status` + `git diff` vor dem Commit
      geprüft)
- [ ] Neues Verhalten hat einen Test
- [ ] `CHANGELOG.md` bei sichtbaren Änderungen ergänzt
- [ ] PR-Beschreibung folgt der Vorlage (`.github/pull_request_template.md`)

Bei Sicherheitsfragen (nicht bei normalen Bugs): siehe `CODE_OF_CONDUCT.md`, Abschnitt
"Sicherheitslücken melden" — nicht als öffentliches Issue.
