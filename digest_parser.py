"""
Parser für usedigest.com Digest-E-Mails (X/Twitter-Posts).

Extrahiert aus der HTML-Mail alle Posts als flache Liste:
    {author, handle, text, url, meta}

Zitierte Posts (Quote-Tweets) erscheinen als eigene Einträge direkt nach
dem zitierenden Post — für die LLM-Synthese ist das ausreichend, da der
inhaltliche Zusammenhang über die Reihenfolge erhalten bleibt.

Getestet gegen das Mailformat von Juli 2026. Die relevanten CSS-Klassen:
    c-sh  Sektions-Header (z.B. "Follows")
    c-nm  Autorname
    c-un  @handle
    c-tt  Post-Text
    c-ms  Metadaten (likes · reposts · Datum)
Ändert usedigest sein Template, müssen diese Anker angepasst werden.
"""

import email
from email import policy
import re

from bs4 import BeautifulSoup


def extract_html_from_eml(eml_bytes: bytes) -> str | None:
    """Holt den text/html-Part aus einer rohen E-Mail."""
    msg = email.message_from_bytes(eml_bytes, policy=policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    return None


def _cls(tag, name):
    classes = tag.get("class") or []
    return name in classes


def parse_digest_html(html: str) -> list[dict]:
    """
    Extrahiert alle X-Posts aus dem Digest-HTML.
    Rückgabe: Liste von {author, handle, text, url, meta} in Dokumentreihenfolge.
    """
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    # Alle Autor-Zellen in Dokumentreihenfolge; von dort aus die zugehörigen
    # Felder einsammeln. Das ist robuster als über die (im HTML invaliden,
    # verschachtelten) <a>-Tags zu gehen.
    author_cells = [td for td in soup.find_all("td") if _cls(td, "c-nm")]

    for author_td in author_cells:
        author = author_td.get_text(strip=True)

        # Handle: nächste c-un-Zelle nach dem Autor
        handle_td = author_td.find_next("td", class_="c-un")
        handle = handle_td.get_text(strip=True) if handle_td else ""

        # Post-URL: nächstliegender umschließender oder vorangehender
        # Status-Link
        url = ""
        anchor = author_td.find_parent("a")
        if anchor and "/status/" in (anchor.get("href") or ""):
            url = anchor["href"]
        else:
            prev_a = author_td.find_previous("a", href=re.compile(r"/status/"))
            if prev_a:
                url = prev_a["href"]

        # Text: nächste c-tt-Zelle — aber nur, wenn dazwischen nicht schon
        # der nächste Autor beginnt (Posts ohne Text, z.B. reine Bilder)
        text = ""
        tt = author_td.find_next("td", class_="c-tt")
        if tt:
            next_author = author_td.find_next("td", class_="c-nm")
            # Kommt der Text VOR dem nächsten Autor im Dokument?
            if next_author is None or _comes_before(tt, next_author):
                text = tt.get_text(" ", strip=True)

        # Metadaten (likes · reposts · Datum): gleiche Logik
        meta = ""
        ms = author_td.find_next("td", class_="c-ms")
        if ms:
            next_author = author_td.find_next("td", class_="c-nm")
            if next_author is None or _comes_before(ms, next_author):
                meta = ms.get_text(" ", strip=True)

        posts.append({
            "author": author,
            "handle": handle,
            "text": text,
            "url": url,
            "meta": meta,
        })

    return posts


def _comes_before(tag_a, tag_b) -> bool:
    """True, wenn tag_a im Dokument vor tag_b steht."""
    # sourceline ist beim html.parser verfügbar; Fallback über Iteration
    if tag_a.sourceline is not None and tag_b.sourceline is not None:
        if tag_a.sourceline != tag_b.sourceline:
            return tag_a.sourceline < tag_b.sourceline
        return (tag_a.sourcepos or 0) < (tag_b.sourcepos or 0)
    # Fallback: tag_b unter den Nachfolgern von tag_a?
    return tag_b in tag_a.find_all_next()


def posts_to_prompt_block(posts: list[dict], max_text_len: int = 500) -> str:
    """Formatiert die Posts kompakt für den LLM-Prompt."""
    lines = []
    for i, p in enumerate(posts, 1):
        text = p["text"][:max_text_len] if p["text"] else "[Bild/Video ohne Text]"
        lines.append(
            f"[{i}] {p['author']} ({p['handle']}): {text}"
            + (f" | {p['meta']}" if p["meta"] else "")
            + (f" | {p['url']}" if p["url"] else "")
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        html = extract_html_from_eml(f.read())
    posts = parse_digest_html(html)
    print(f"{len(posts)} Posts extrahiert:\n")
    for p in posts[:10]:
        print(f"- {p['author']} {p['handle']}: {p['text'][:80]!r}")
        print(f"    meta={p['meta']!r}")
        print(f"    url={p['url'][:70]}")
