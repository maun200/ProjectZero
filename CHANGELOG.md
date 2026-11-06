# Changelog

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert. Das Format orientiert
sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [0.3.0] – Unreleased

### Hinzugefügt

- Self-Registration für Mitglieder (`CAG_REGISTRATION=off|invite|open`) mit Einladungscodes
  (mehrfach verwendbar, mit Ablaufzeit) als Alternative zur reinen Admin-Erstellung.
- Mitgliederkonten: eigener Anzeigename, `/api/me` (Guthaben + eigene Maschinen), Umbenennen,
  Token-Rotation.
- Mitglieder können eigene Maschinen als `community`-Worker anmelden und wieder entfernen
  (begrenzt durch `CAG_MAX_MACHINES_PER_MEMBER`).
- Online-Liste: welche Maschinen und Mitglieder aktuell aktiv sind.
- Lobby-, Gruppen- und Direktnachrichten-Kanäle (alles Klartext, siehe `docs/SECURITY.md` —
  das ist explizit **kein** privater Messenger).
- Optionales `CAG_AUTO_SETTLE`: Guthaben aus abgeschlossenen Jobs sofort (statt provisorisch)
  gutschreiben — weiterhin ohne kryptographische Verifikation.
- Self-Enrollment-Codes (`/api/enroll`) sind jetzt strikt einmalig verwendbar (Tabelle
  `enroll_codes`, atomar mit der Worker-Erstellung verbraucht) und zusätzlich pro Quell-IP
  gedrosselt (`CAG_ENROLL_IP_PER_MINUTE`).
- Worker-Selbstauskunft (`GET /api/worker/me`): eigener Status/Guthaben ohne Zugriff auf das
  getrennte Nutzer-Token.
- Lokaler Client (in Arbeit): plattformübergreifende Installation mit Ein-Zeilen-Installern für
  Linux/macOS/Windows, Chat-Oberfläche mit Guthabenanzeige und Reglern für die eigenen
  Ressourcenlimits.

## [0.2.0]

### Hinzugefügt

- Worker-Token-Widerruf und -Rotation.
- Ratenbegrenzung pro authentifizierter Identität und Coordinator-Prozess.
- Job-Abbruch durch Nutzer:innen (`/api/jobs/{id}/cancel`) mit Rückerstattung.
- Warteschlangen-Timeout mit automatischer Rückerstattung.
- Hintergrund-Sweeper für abgelaufene Leases und veraltete Warteschlangeneinträge.
- Operator-Modell-Allowlist (`CAG_ALLOWED_MODELS`) für Jobs und Worker-Anmeldung.
- Automatisierte Tests und CI.

## [0.1.0]

### Hinzugefügt

- Erster Prototyp: zentraler Coordinator (FastAPI + SQLite), persistente Jobs, provisorisches
  Guthabenkonto pro Worker.
- Worker ziehen Jobs aktiv ab ("Pull") und rufen einen lokalen, OpenAI-kompatiblen LLM-Endpunkt
  auf — führen aber keinen heruntergeladenen Code aus.
- Bearer-Credentials pro Worker, Modellabgleich, vertrauensbewusste Zuteilung (`trusted` vs.
  `community`), Lease- und Heartbeat-Mechanismus mit Wiederholungsversuchen und Rückerstattung.
- Browser-Test-UI unter `/` und OpenAPI-Dokumentation unter `/docs`.
- Mock-Backend für CPU-only-Orchestrierungstests ohne echte KI-Inferenz.
