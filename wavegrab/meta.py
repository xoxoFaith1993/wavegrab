"""Song metadata: works out what a track really is and writes full ID3 tags.

Sources, best first:
  * Spotify (for Spotify links): title, artists, exact release date, album art
  * iTunes Search API (free, no key): album, album artist, genre, track/disc numbers,
    release date and 1000px art - only used when title, artist AND length agree
  * yt-dlp's own music fields (YouTube Music / "- Topic" uploads)
"""
import difflib
import io
import json
import re
import unicodedata
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Bits of YouTube titles that aren't part of the song name
_JUNK = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*\b(official|video|audio|lyric|lyrics|visuali[sz]er|hd|hq|4k|mv|m/v|clip|"
    r"music video|explicit|clean|full song|out now|premiere)\b[^\)\]\}]*[\)\]\}]",
    re.I)
_VERSION_WORDS = ["remix", "live", "acoustic", "instrumental", "karaoke", "cover", "sped up", "slowed",
                  "demo", "edit", "mix", "version", "remaster", "mono", "stereo", "extended", "reprise"]


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(10_000_000)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)            # drop bracketed parts
    s = re.sub(r"\s+-\s+.*$", "", s)                     # "Song - Remastered 2011"
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def clean_title(title):
    t = _JUNK.sub("", title or "")
    t = re.sub(r"\s*\|\s*.*$", "", t)  # "Song | Channel name"
    return re.sub(r"\s{2,}", " ", t).strip(" -–—")


def guess_song(info):
    """From a yt-dlp info dict, guess (artist, title, is_music)."""
    track = info.get("track")
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else None)
    if track and artists:
        return ", ".join(artists), track, True
    channel = (info.get("channel") or info.get("uploader") or "").strip()
    title = info.get("title") or ""
    if channel.endswith(" - Topic"):
        return channel[:-8], clean_title(title), True
    m = re.match(r"^\s*(.+?)\s+[-–—]\s+(.+)$", title)
    if m:
        return m.group(1).strip(), clean_title(m.group(2)), True
    channel = re.sub(r"(VEVO|Official|Music|TV)$", "", channel).strip()
    is_music = "music" in " ".join(info.get("categories") or []).lower()
    return channel, clean_title(title), is_music


# ------------------------------------------------------------------ iTunes

def itunes_match(title, artist, duration=None):
    """Find the same recording in Apple's catalogue. Returns a dict or None."""
    first_artist = re.split(r",|&| x | feat\.? | ft\.? ", artist or "", flags=re.I)[0].strip()
    term = f"{first_artist} {norm(title)}".strip()
    if not term:
        return None
    q = urllib.parse.urlencode({"term": term, "media": "music", "entity": "song", "limit": 15})
    try:
        results = json.loads(_get("https://itunes.apple.com/search?" + q))["results"]
    except Exception:
        return None
    wanted_versions = [w for w in _VERSION_WORDS if w in (title or "").lower()]
    best, best_score = None, 0.0
    for r in results:
        t_sim = similar(r.get("trackName"), title)
        a_sim = max(similar(r.get("artistName"), first_artist),
                    1.0 if norm(first_artist) and norm(first_artist) in norm(r.get("artistName")) else 0)
        if t_sim < 0.8 or a_sim < 0.75:
            continue
        score = t_sim * 2 + a_sim
        if duration and r.get("trackTimeMillis"):
            diff = abs(r["trackTimeMillis"] / 1000 - duration)
            if diff > 6:
                continue
            score -= diff * 0.1
        name = (r.get("trackName") or "").lower() + " " + (r.get("collectionName") or "").lower()
        for w in _VERSION_WORDS:
            if w in name and w not in wanted_versions:
                score -= 0.4
        if "compilation" in (r.get("collectionName") or "").lower() or r.get("collectionArtistName") == "Various Artists":
            score -= 0.3
        if score > best_score:
            best, best_score = r, score
    if not best:
        return None
    art = best.get("artworkUrl100")
    return {
        "title": best.get("trackName"),
        "artist": best.get("artistName"),
        "album": best.get("collectionName"),
        "album_artist": best.get("collectionArtistName") or best.get("artistName"),
        "genre": best.get("primaryGenreName"),
        "date": (best.get("releaseDate") or "")[:10] or None,
        "track_no": best.get("trackNumber"),
        "track_total": best.get("trackCount"),
        "disc_no": best.get("discNumber"),
        "disc_total": best.get("discCount"),
        "cover": art.replace("100x100bb", "1000x1000bb") if art else None,
    }


# ------------------------------------------------------------------ build tag sets

def _date_from_ytdlp(info, music):
    rd = info.get("release_date")
    if rd and len(rd) == 8:
        return f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
    if info.get("release_year"):
        return str(info["release_year"])
    if not music:
        ud = info.get("upload_date")
        if ud and len(ud) == 8:
            return f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
    return None


def for_youtube(info):
    """Tag set for a YouTube download (song or not)."""
    artist, title, music = guess_song(info)
    meta = {"title": title or info.get("title"), "artist": artist,
            "date": _date_from_ytdlp(info, music), "album": info.get("album"),
            "album_artist": info.get("album_artist"), "track_no": info.get("track_number"),
            "genre": info.get("genre"), "cover": info.get("thumbnail"), "square": False,
            "source": info.get("webpage_url"), "music": music}
    hit = itunes_match(title, artist, info.get("duration")) if (artist and title) else None
    if hit:
        meta.update({k: v for k, v in hit.items() if v})
        meta["square"] = True
        meta["music"] = True
    elif music:
        meta["square"] = True  # centre-crop the video thumbnail to square album art
    if not meta.get("album_artist") and meta["music"]:
        meta["album_artist"] = (artist or "").split(",")[0].strip() or None
    return meta


def for_spotify(item, spotify_track):
    """Tag set for a Spotify track. spotify_track = data from its embed page (may be None)."""
    st = spotify_track or {}
    artists = st.get("artists") or item.get("artist") or ""
    title = st.get("title") or item["title"]
    meta = {"title": title, "artist": artists, "date": st.get("date"),
            "album": item.get("album"), "album_artist": item.get("album_artist"),
            "track_no": item.get("track_no"), "track_total": item.get("track_total"),
            "cover": st.get("cover") or item.get("thumb"), "square": True, "music": True,
            "source": f"https://open.spotify.com/track/{item['track_id']}" if item.get("track_id") else None}
    hit = itunes_match(title, artists, st.get("duration") or item.get("duration"))
    if hit:
        meta["genre"] = hit["genre"]
        if not meta["album"]:
            # Track came from a playlist/single link: borrow Apple's album info
            for k in ("album", "album_artist", "track_no", "track_total", "disc_no", "disc_total"):
                meta[k] = hit.get(k)
        if not meta["date"]:
            meta["date"] = hit["date"]
        if not meta["cover"]:
            meta["cover"] = hit["cover"]
    if not meta.get("album_artist"):
        meta["album_artist"] = artists.split(",")[0].strip() or None
    return meta


# ------------------------------------------------------------------ writing

def _cover_jpeg(url, square):
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(_get(url))).convert("RGB")
    if square and abs(im.width - im.height) > 4:
        # YouTube thumbnails are 16:9; many have the square art in the middle
        side = min(im.size)
        im = ImageOps.fit(im, (side, side), Image.LANCZOS)
    if max(im.size) > 1200:
        im.thumbnail((1200, 1200), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def write_mp3(path, meta):
    from mutagen.id3 import (APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, WOAS)
    tags = ID3()  # fresh tag: replaces anything ffmpeg wrote
    if meta.get("title"):
        tags.add(TIT2(encoding=3, text=meta["title"]))
    if meta.get("artist"):
        tags.add(TPE1(encoding=3, text=meta["artist"]))
    if meta.get("album"):
        tags.add(TALB(encoding=3, text=meta["album"]))
    if meta.get("album_artist"):
        tags.add(TPE2(encoding=3, text=meta["album_artist"]))
    if meta.get("track_no"):
        n = str(meta["track_no"]) + (f"/{meta['track_total']}" if meta.get("track_total") else "")
        tags.add(TRCK(encoding=3, text=n))
    if meta.get("disc_no"):
        n = str(meta["disc_no"]) + (f"/{meta['disc_total']}" if meta.get("disc_total") else "")
        tags.add(TPOS(encoding=3, text=n))
    if meta.get("genre"):
        tags.add(TCON(encoding=3, text=meta["genre"]))
    if meta.get("date"):
        tags.add(TDRC(encoding=3, text=meta["date"]))
    if meta.get("source"):
        tags.add(WOAS(url=meta["source"]))
    if meta.get("cover"):
        try:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                          data=_cover_jpeg(meta["cover"], meta.get("square", True))))
        except Exception:
            pass
    # v2.3 is what Windows Explorer, phones and car stereos read most reliably;
    # mutagen turns the full date into Year (TYER) + day/month (TDAT).
    tags.save(path, v2_version=3)
