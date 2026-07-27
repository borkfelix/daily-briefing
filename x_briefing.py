"""
X-Briefing-Sektion: holt die neueste usedigest-Mail per IMAP, extrahiert
die Posts und destilliert sie per Claude in Kernerkenntnisse pro Kategorie.

Benötigte Env-Variablen (alle optional — fehlen sie, wird die X-Sektion
einfach übersprungen):
    IMAP_HOST   z.B. imap.gmail.com
    IMAP_USER   Postfach, in dem die Digest-Mails landen
    IMAP_PASS   App-Passwort
    IMAP_FOLDER optional, Default INBOX

Kategorien werden in channels.yaml unter dem Key x_categories definiert:
    x_categories:
      - "AI"
      - "Health"
      - "News / Politik"
"""

import email
import imaplib
import os
import re

from digest_parser import extract_html_from_eml, parse_digest_html, posts_to_prompt_block

DIGEST_FROM_HINT = "usedigest"   # matcht Absender wie ...usedigest_com...@simplelogin.co
DEFAULT_CATEGORIES = ["AI", "Health", "News / Politik"]


def imap_configured() -> bool:
    return all(os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"))


def fetch_latest_digest_eml() -> bytes | None:
    """Holt die neueste Digest-Mail (roh) aus dem IMAP-Postfach."""
    host = os.environ["IMAP_HOST"]
    user = os.environ["IMAP_USER"]
    password = os.environ["IMAP_PASS"]
    folder = os.environ.get("IMAP_FOLDER", "INBOX")

    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder, readonly=True)

        # Neueste Mail, deren Absender auf usedigest hindeutet.
        # SINCE-Filter auf heute wäre strenger, aber Zeitzonen machen das
        # fehleranfällig; wir nehmen die letzte passende Mail und prüfen
        # das Datum nicht hart — die Synthese eines 1 Tag alten Digests
        # ist besser als gar keine X-Sektion.
        status, data = imap.search(None, "FROM", DIGEST_FROM_HINT)
        if status != "OK" or not data or not data[0]:
            return None
        ids = data[0].split()
        latest_id = ids[-1]
        status, msg_data = imap.fetch(latest_id, "(RFC822)")
        if status != "OK":
            return None
        return msg_data[0][1]


def synthesize_x_section(client, model, posts, categories) -> str | None:
    """
    Ein Claude-Call: alle Posts rein, pro Kategorie 3-5 Kernerkenntnisse
    raus. Rückgabe: fertiges HTML-Fragment oder None.
    """
    if not posts:
        return None

    block = posts_to_prompt_block(posts)
    cat_list = ", ".join(categories)
    prompt = (
        f"Hier sind die heutigen X-Posts aus meinem Feed "
        f"({len(posts)} Posts, teils mit zitierten Posts direkt darunter):\n\n"
        f"{block}\n\n"
        f"Aufgabe: Destilliere daraus ein tägliches Briefing.\n"
        f"Kategorien: {cat_list}\n\n"
        f"Regeln:\n"
        f"- Pro Kategorie die 3-5 wichtigsten Erkenntnisse/Entwicklungen des "
        f"Tages als knappe Bulletpoints (Format: '- Punkt').\n"
        f"- Hinter jeden Punkt die Nummer(n) der Quell-Posts in eckigen "
        f"Klammern, z.B. [3] oder [3,12].\n"
        f"- Ignoriere Memes, reine Bild-Posts ohne Kontext, Selbstpromo und "
        f"Belanglosigkeiten.\n"
        f"- Behandle Behauptungen in Posts als Behauptungen, nicht als "
        f"Fakten (Formulierungen wie 'X behauptet, dass...').\n"
        f"- Kategorien ohne relevante Posts weglassen.\n"
        f"- Antworte NUR mit den Kategorien und Bulletpoints, Format:\n"
        f"### Kategoriename\\n- Punkt [n]\\n- Punkt [n]\n"
        f"- Deutsch, sachlich, keine Einleitung."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    summary_md = resp.content[0].text.strip()
    return _markdown_to_html(summary_md, posts)


def _markdown_to_html(md: str, posts) -> str:
    """Wandelt die Kategorie-Bullets in HTML um; [n]-Referenzen werden
    zu Links auf die Original-Posts."""

    def ref_to_links(match):
        nums = re.findall(r"\d+", match.group(0))
        links = []
        for n in nums:
            idx = int(n) - 1
            if 0 <= idx < len(posts) and posts[idx]["url"]:
                handle = posts[idx]["handle"] or f"#{n}"
                links.append(
                    f'<a href="{posts[idx]["url"]}" '
                    f'style="color:#888;text-decoration:none;">{handle}</a>'
                )
        return " " + " ".join(links) if links else ""

    html_parts = []
    for line in md.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("###"):
            title = line.lstrip("#").strip()
            html_parts.append(
                f'<div style="font-size:15px;font-weight:600;'
                f'margin:16px 0 6px;">{title}</div>'
            )
        elif line.startswith("-"):
            content = line.lstrip("-").strip()
            content = re.sub(r"\[[\d,\s]+\]", ref_to_links, content)
            html_parts.append(
                f'<div style="font-size:14px;line-height:1.5;'
                f'margin:0 0 6px 12px;">• {content}</div>'
            )
        else:
            html_parts.append(
                f'<div style="font-size:14px;line-height:1.5;">{line}</div>'
            )
    return "\n".join(html_parts)


def build_x_briefing(client, model, categories=None) -> str | None:
    """
    Komplettablauf: IMAP → Parser → Synthese.
    Gibt HTML-Fragment für die Briefing-Mail zurück, oder None wenn
    IMAP nicht konfiguriert ist oder keine Digest-Mail gefunden wurde.
    Wirft keine Exceptions nach außen — die X-Sektion darf das
    YouTube-Briefing nie blockieren.
    """
    if not imap_configured():
        print("X-Sektion: IMAP nicht konfiguriert, übersprungen.")
        return None
    try:
        eml = fetch_latest_digest_eml()
        if not eml:
            print("X-Sektion: keine Digest-Mail gefunden.")
            return None
        html = extract_html_from_eml(eml)
        if not html:
            print("X-Sektion: Mail ohne HTML-Part.")
            return None
        posts = parse_digest_html(html)
        print(f"X-Sektion: {len(posts)} Posts extrahiert.")
        if not posts:
            return None
        return synthesize_x_section(
            client, model, posts, categories or DEFAULT_CATEGORIES
        )
    except Exception as e:
        print(f"X-Sektion fehlgeschlagen (Briefing läuft ohne sie weiter): {e}")
        return None
