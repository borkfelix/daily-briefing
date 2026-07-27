"""
Löst eine Liste von YouTube-Kanal-URLs oder @Handles zu Channel-IDs auf
und schreibt direkt eine channels.yaml.

Nutzung:
    1. Lege eine Textdatei an (z.B. urls.txt), eine URL/Handle pro Zeile, z.B.:
         https://www.youtube.com/@lexfridman
         https://www.youtube.com/@veritasium
         @some_other_channel

    2. pip install -r requirements.txt

    3. python resolve_channel_id.py urls.txt

Erzeugt channels.yaml mit priority: medium für alle Kanäle.
Priority danach von Hand in der Datei anpassen (high für deine Lieblingskanäle).
"""

import sys
import yt_dlp


def resolve(url_or_handle: str):
    if url_or_handle.startswith("@"):
        url_or_handle = f"https://www.youtube.com/{url_or_handle}"

    ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url_or_handle, download=False)
        channel_id = info.get("channel_id") or info.get("id")
        name = info.get("channel") or info.get("title") or channel_id
        return channel_id, name


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python resolve_channel_id.py urls.txt")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    results = []
    for line in lines:
        try:
            cid, name = resolve(line)
            results.append((name, cid))
            print(f"OK:     {name} -> {cid}")
        except Exception as e:
            print(f"FEHLER: {line} -> {e}")

    with open("channels.yaml", "w", encoding="utf-8") as f:
        f.write("channels:\n")
        for name, cid in results:
            safe_name = name.replace('"', "'")
            f.write(f'  - name: "{safe_name}"\n    id: "{cid}"\n    priority: medium\n')

    print(f"\n{len(results)} Kanäle in channels.yaml geschrieben.")
    print("Setze bei deinen Lieblingskanälen priority auf 'high'.")


if __name__ == "__main__":
    main()
