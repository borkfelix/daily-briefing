"""
Tägliches YouTube-Briefing — Version 2 (kostenoptimiert).

Änderungen gegenüber v1:
- EIN Triage-Call für alle Titel des Tages (statt ein Call pro Video)
- Zusammenfassungen laufen über die Message Batches API (50% günstiger)
- Standardmodell: Haiku (günstig, für Bulletpoint-Summaries völlig ausreichend)
- Transkript-Fetch mit Retry + Backoff

Ablauf:
1. channels.yaml lesen, RSS-Feeds abrufen (kostenlos, keine API-Quota)
2. Neue Videos gegen seen_videos.json ermitteln
3. Ein einziger Triage-Call: alle Titel rein -> relevante IDs als JSON raus
   (high-priority Kanäle überspringen die Triage, werden immer zusammengefasst)
4. Transkripte holen (mit Retry/Backoff)
5. Ein Batch-Request mit allen Zusammenfassungen -> pollen bis fertig
6. HTML-Mail bauen und versenden
7. seen_videos.json fortschreiben (Workflow committed sie zurück ins Repo)
"""

import os
import json
import time
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import xml.etree.ElementTree as ET

import requests
import yaml
import anthropic
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)

from x_briefing import build_x_briefing

STATE_FILE = "seen_videos.json"
CHANNELS_FILE = "channels.yaml"
MAX_SEEN_PER_CHANNEL = 60          # State-Datei klein halten
MAX_TRANSCRIPT_CHARS = 60_000      # ~15-20k Tokens; längere Transkripte werden gekürzt
TRANSCRIPT_RETRIES = 3
BATCH_POLL_SECONDS = 30
BATCH_TIMEOUT_SECONDS = 60 * 60    # 1h; Batches sind meist nach Minuten fertig

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


# ---------------------------------------------------------------- Grundlagen

def load_config():
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["channels"], cfg.get("x_categories")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fetch_rss(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    entries = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.find("yt:videoId", NS).text
        title = entry.find("atom:title", NS).text
        entries.append({"video_id": vid, "title": title})
    return entries


# ---------------------------------------------------------------- Schritt 1+2

def collect_new_videos(channels, state):
    """Liefert Liste von dicts: video_id, title, channel, priority."""
    new_videos = []
    for ch in channels:
        try:
            entries = fetch_rss(ch["id"])
        except Exception as e:
            print(f"RSS-Fehler bei {ch['name']}: {e}")
            continue

        seen = set(state.get(ch["id"], []))
        for e in entries:
            if e["video_id"] in seen:
                continue
            state.setdefault(ch["id"], []).append(e["video_id"])
            new_videos.append({
                "video_id": e["video_id"],
                "title": e["title"],
                "channel": ch["name"],
                "priority": ch.get("priority", "medium"),
            })
        state[ch["id"]] = state.get(ch["id"], [])[-MAX_SEEN_PER_CHANNEL:]
    return new_videos


# ---------------------------------------------------------------- Schritt 3

def triage_all(videos):
    """
    EIN Call für alle Nicht-high-priority-Videos.
    Gibt die Menge der als relevant eingestuften video_ids zurück.
    """
    candidates = [v for v in videos if v["priority"] != "high"]
    if not candidates:
        return set()

    listing = "\n".join(
        f"- id: {v['video_id']} | Kanal: {v['channel']} | Titel: {v['title']}"
        for v in candidates
    )
    prompt = (
        "Hier ist eine Liste neuer YouTube-Videos (id, Kanal, Titel):\n\n"
        f"{listing}\n\n"
        "Wähle die Videos aus, die für ein tägliches News-Briefing inhaltlich "
        "substanziell wirken. Aussortieren: reiner Clickbait, Werbung, "
        "Shorts/Teaser ohne Inhalt, Stream-Ankündigungen, Musik.\n"
        "Antworte NUR mit einem JSON-Array der relevanten ids, z.B. "
        '["abc123","def456"]. Kein anderer Text.'
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # JSON-Array robust extrahieren, auch wenn das Modell drumherum redet
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        print(f"Triage: unerwartete Antwort, nehme alle Kandidaten. Antwort: {text[:200]}")
        return {v["video_id"] for v in candidates}
    try:
        ids = json.loads(text[start:end + 1])
        return set(ids)
    except json.JSONDecodeError:
        print("Triage: JSON nicht parsebar, nehme alle Kandidaten.")
        return {v["video_id"] for v in candidates}


# ---------------------------------------------------------------- Schritt 4

def get_transcript(video_id):
    for attempt in range(TRANSCRIPT_RETRIES):
        try:
            segments = YouTubeTranscriptApi.get_transcript(
                video_id, languages=["de", "en"]
            )
            return " ".join(s["text"] for s in segments)
        except (TranscriptsDisabled, NoTranscriptFound):
            return None  # gibt es wirklich nicht, kein Retry nötig
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  Transkript-Fehler {video_id} (Versuch {attempt+1}): {e} — warte {wait}s")
            time.sleep(wait)
    return None


# ---------------------------------------------------------------- Schritt 5

def summarize_batch(items):
    """
    items: Liste von dicts mit video_id, title, channel, transcript.
    Gibt dict video_id -> summary zurück. Nutzt die Message Batches API.
    """
    if not items:
        return {}

    batch_requests = []
    for it in items:
        prompt = (
            f"Kanal: {it['channel']}\nTitel: {it['title']}\n\n"
            f"Transkript:\n{it['transcript'][:MAX_TRANSCRIPT_CHARS]}\n\n"
            "Fasse die wichtigsten Punkte in 3-5 knappen Bulletpoints zusammen "
            "(Format: '- Punkt'). Deutsch, sachlich, keine Einleitung, "
            "keine Meta-Kommentare."
        )
        batch_requests.append({
            "custom_id": it["video_id"],
            "params": {
                "model": MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
        })

    batch = client.messages.batches.create(requests=batch_requests)
    print(f"Batch {batch.id} mit {len(batch_requests)} Requests erstellt. Warte auf Ergebnis...")

    waited = 0
    while waited < BATCH_TIMEOUT_SECONDS:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(BATCH_POLL_SECONDS)
        waited += BATCH_POLL_SECONDS
    else:
        print("Batch-Timeout — verschicke, was bis jetzt da ist (nichts).")
        return {}

    summaries = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            summaries[result.custom_id] = result.result.message.content[0].text.strip()
        else:
            print(f"  Batch-Item {result.custom_id} fehlgeschlagen: {result.result.type}")
    return summaries


# ---------------------------------------------------------------- Schritt 6

def build_email_html(high_items, other_items, no_transcript, x_section_html=None):
    def video_url(vid):
        return f"https://www.youtube.com/watch?v={vid}"

    def render_item(item):
        bullets = item["summary"].replace("\n", "<br>")
        return f"""
        <div style="margin-bottom:24px;padding:16px;border:1px solid #ddd;border-radius:8px;">
          <div style="font-size:13px;color:#666;">{item['channel']}</div>
          <div style="font-size:16px;font-weight:600;margin:4px 0;">
            <a href="{video_url(item['video_id'])}" style="color:#111;text-decoration:none;">{item['title']}</a>
          </div>
          <div style="font-size:14px;line-height:1.5;">{bullets}</div>
        </div>
        """

    html = ""
    if x_section_html:
        html += "<h2>X — Themen des Tages</h2>" + x_section_html

    html += "<h2>Wichtigste Videos</h2>"
    if high_items:
        html += "".join(render_item(i) for i in high_items)
    else:
        html += "<p>Heute nichts Neues von deinen Top-Kanälen.</p>"

    if other_items:
        html += "<h2>Weitere relevante Videos</h2>"
        html += "".join(render_item(i) for i in other_items)

    if no_transcript:
        html += "<h2>Ohne Transkript (übersprungen)</h2><ul>"
        for channel, title, vid in no_transcript:
            html += f'<li>{channel} – <a href="{video_url(vid)}">{title}</a></li>'
        html += "</ul>"

    return html


def send_email(html_body, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as server:
        server.starttls(context=context)
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        server.send_message(msg)


# ---------------------------------------------------------------- Main

def main():
    channels, x_categories = load_config()
    state = load_state()

    # --- X-Sektion (unabhängig vom YouTube-Teil; None wenn nicht konfiguriert)
    x_section_html = build_x_briefing(client, MODEL, x_categories)

    new_videos = collect_new_videos(channels, state)
    print(f"{len(new_videos)} neue Videos gefunden.")

    # State sofort sichern: auch bei späterem Fehler werden Videos nicht
    # doppelt verarbeitet; lieber ein Video verpassen als Endlos-Duplikate.
    save_state(state)

    if not new_videos and not x_section_html:
        print("Nichts Neues heute. Keine Mail.")
        return

    relevant_ids = triage_all(new_videos)
    to_process = [
        v for v in new_videos
        if v["priority"] == "high" or v["video_id"] in relevant_ids
    ]
    print(f"{len(to_process)} Videos nach Triage (davon "
          f"{sum(1 for v in to_process if v['priority'] == 'high')} high-priority).")

    with_transcript, no_transcript = [], []
    for v in to_process:
        transcript = get_transcript(v["video_id"])
        time.sleep(1.5)  # sanftes Rate-Limiting gegen IP-Blocks
        if transcript:
            v["transcript"] = transcript
            with_transcript.append(v)
        else:
            no_transcript.append((v["channel"], v["title"], v["video_id"]))

    summaries = summarize_batch(with_transcript)

    high_items, other_items = [], []
    for v in with_transcript:
        if v["video_id"] not in summaries:
            continue
        item = {
            "channel": v["channel"],
            "title": v["title"],
            "video_id": v["video_id"],
            "summary": summaries[v["video_id"]],
        }
        (high_items if v["priority"] == "high" else other_items).append(item)

    if not high_items and not other_items and not no_transcript and not x_section_html:
        print("Nach Verarbeitung nichts übrig. Keine Mail.")
        return

    html = build_email_html(high_items, other_items, no_transcript, x_section_html)
    subject = f"Daily Briefing – {datetime.now().strftime('%d.%m.%Y')}"
    send_email(html, subject)
    print(f"Mail gesendet: {len(high_items)} Top-Videos, {len(other_items)} weitere, "
          f"{len(no_transcript)} ohne Transkript, "
          f"X-Sektion: {'ja' if x_section_html else 'nein'}.")


if __name__ == "__main__":
    main()
