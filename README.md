# YouTube Daily Briefing

Holt täglich neue Videos deiner abonnierten Kanäle, fasst relevante Transkripte
zusammen und schickt dir ein E-Mail-Briefing. Läuft komplett kostenlos auf
GitHub Actions -- kein eigener Server nötig.

## Setup (einmalig, ca. 20 Minuten)

### 1. Repo erstellen
Lade diesen Ordner in ein **eigenes GitHub-Repo** hoch (privates Repo empfohlen,
da darin deine Kanalliste und der State liegen).

### 2. Kanalliste erzeugen
Du hast über 100 Kanäle -- die IDs von Hand raussuchen wäre nervig. Nutze den
Helper:

```bash
pip install -r requirements.txt

# Erstelle eine urls.txt mit einer Kanal-URL/Handle pro Zeile, z.B.:
# https://www.youtube.com/@lexfridman
# @veritasium
# https://www.youtube.com/@some_channel

python resolve_channel_id.py urls.txt
```

Das erzeugt `channels.yaml`. Öffne die Datei danach und setze bei deinen
wichtigsten Kanälen `priority: high` (die bekommen immer eine volle
Zusammenfassung -- alle anderen werden erst per günstigem Titel-Check gefiltert).

**Tipp, um an deine Abo-Liste zu kommen:** Google Takeout
(takeout.google.com) -> YouTube und YouTube Musik -> "Abonnements" exportieren,
liefert eine CSV mit allen Kanal-URLs.

### 3. Secrets im GitHub-Repo hinterlegen
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Wert |
|---|---|
| `ANTHROPIC_API_KEY` | Dein Anthropic API-Key (console.anthropic.com) |
| `SMTP_HOST` | z.B. `smtp.gmail.com` |
| `SMTP_PORT` | z.B. `587` |
| `SMTP_USER` | Deine E-Mail-Adresse |
| `SMTP_PASS` | App-Passwort (siehe unten) |
| `EMAIL_TO` | Wohin das Briefing gehen soll |

**Gmail App-Passwort:** Falls du Gmail nutzt, brauchst du ein "App-Passwort"
(nicht dein normales Passwort). Google-Konto -> Sicherheit ->
2-Faktor-Authentifizierung aktivieren -> App-Passwörter -> neues erstellen.

### 4. channels.yaml committen
`channels.yaml` und die leere `seen_videos.json` mit ins Repo committen und
pushen.

### 5. Testen
Repo -> Actions -> "Daily YouTube Briefing" -> "Run workflow" (manueller Trigger).
Danach die Logs checken, ob alles durchläuft. Beim allerersten Lauf gibt's viele
"neue" Videos (alles, was aktuell im RSS-Feed jedes Kanals steht) -- das kann
ein bisschen dauern und eine größere erste Mail geben. Ab dem zweiten Lauf ist
es dann wirklich nur noch das Neue vom Tag.

## Wie es läuft (v2, kostenoptimiert)

- **Cron:** jeden Tag um 5:00 UTC (in `.github/workflows/daily-briefing.yml`
  anpassbar an deine Zeitzone)
- **Neue Videos erkennen:** über den kostenlosen RSS-Feed jedes Kanals, keine
  API-Quota nötig
- **Triage in EINEM Call:** alle neuen Titel des Tages gehen gebündelt in einen
  einzigen günstigen Haiku-Request, der die relevanten IDs als JSON zurückgibt.
  `priority: high` Kanäle überspringen die Triage und werden immer
  zusammengefasst
- **Zusammenfassung per Batch API:** alle Transkripte gehen als EIN
  Batch-Request an die Message Batches API (50% günstiger als
  Einzel-Requests). Das Skript pollt, bis der Batch fertig ist (meist wenige
  Minuten)
- **Transkripte:** mit Retry + Backoff (3 Versuche); Videos ohne Untertitel
  landen als Linkliste am Ende der Mail
- **State:** `seen_videos.json` merkt sich verarbeitete Video-IDs und wird nach
  jedem Lauf zurück ins Repo committed. Der State wird bewusst VOR der
  Verarbeitung gespeichert: schlägt ein Lauf fehl, werden Videos beim nächsten
  Mal nicht doppelt verarbeitet (lieber ein Video verpassen als Duplikate)

## Optional: X-Sektion (Themen des Tages aus deinen X-Follows)

Voraussetzung: ein usedigest.com-Account, der deine X-Accounts als tägliche
Digest-Mail an ein Postfach schickt, auf das das Skript per IMAP zugreifen darf.

1. Bei usedigest.com deine X-Accounts als Quellen hinzufügen, Zustellung
   z.B. auf 4:00 Uhr stellen (vor dem Briefing-Cron um 5:00 UTC)
2. Drei weitere Secrets im Repo hinterlegen:

| Secret | Wert |
|---|---|
| `IMAP_HOST` | z.B. `imap.gmail.com` |
| `IMAP_USER` | Postfach, in dem die Digest-Mails landen |
| `IMAP_PASS` | App-Passwort (bei Gmail dasselbe Verfahren wie für SMTP) |

3. In `channels.yaml` die Kategorien anpassen (`x_categories`), Default:
   AI, Health, News / Politik

Das Skript holt die neueste usedigest-Mail, extrahiert alle Posts
(`digest_parser.py`), und ein einzelner Claude-Call destilliert daraus pro
Kategorie die 3-5 wichtigsten Erkenntnisse des Tages — mit Links zu den
Original-Posts. Die X-Sektion erscheint oben in derselben Briefing-Mail.
Fehlt die IMAP-Konfiguration oder schlägt der Abruf fehl, läuft das
YouTube-Briefing normal ohne sie weiter.

Hinweis: Der Parser ist auf das usedigest-Mailformat von Juli 2026 gebaut
(CSS-Klassen c-nm/c-un/c-tt/c-ms). Ändert usedigest sein Template, muss
`digest_parser.py` angepasst werden — das Skript loggt dann "0 Posts
extrahiert".

## Bekannte Einschränkungen

- Videos ohne Untertitel (deaktiviert, Musik, manche Livestreams) können nicht
  zusammengefasst werden -- die landen als Link am Ende der Mail
- `youtube-transcript-api` ist inoffiziell und kann bei zu aggressiven
  Anfragen von YouTube gedrosselt werden. Das Skript pausiert daher 1,5 Sek.
  zwischen Transkript-Abrufen. Falls es trotzdem zu Fehlern kommt: Pause im
  Skript erhöhen
- Modellname (`ANTHROPIC_MODEL`) ggf. anpassen, falls sich die aktuellen
  Modellbezeichnungen bei Anthropic geändert haben

## Kosten

- GitHub Actions: kostenlos für öffentliche Repos, bei privaten Repos
  2.000 Freiminuten/Monat -- reicht für diesen Task locker (Achtung: durch das
  Batch-Polling läuft der Job einige Minuten länger; bei privaten Repos die
  Freiminuten im Blick behalten)
- Anthropic API (Haiku + Batch API): bei ~100 Kanälen und ~30 zusammengefassten
  Videos/Tag grob 10-15 Cent/Tag, also ca. 3-5 EUR/Monat. Aktuelle Preise:
  https://claude.com/pricing
- Wichtig: Das Claude-Pro-Abo (claude.ai) deckt KEINE API-Nutzung ab -- du
  brauchst separates API-Guthaben über console.anthropic.com
- Modell per Env-Variable `ANTHROPIC_MODEL` änderbar (Default: Haiku). Für
  hochwertigere Zusammenfassungen Sonnet eintragen (ca. 3x teurer)
