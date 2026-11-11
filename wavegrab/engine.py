"""Download engine: wraps yt-dlp + ffmpeg, plus the Spotify -> YouTube matcher."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from wavegrab import meta

APP_NAME = "Wavegrab"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
ENGINE_ZIP = os.path.join(DATA_DIR, "engine", "yt-dlp.zip")
ENGINE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

FROZEN = getattr(sys, "frozen", False)

# In the packaged app there is no console, so stop ffmpeg/deno flashing a black
# window every time they run. Must happen before yt_dlp is imported, because
# yt-dlp subclasses subprocess.Popen at import time.
if os.name == "nt":
    _Popen = subprocess.Popen

    class _QuietPopen(_Popen):
        def __init__(self, *args, **kwargs):
            if not kwargs.get("creationflags"):
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _QuietPopen


def app_dir():
    if FROZEN:
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource(*parts):
    """Path to a bundled file (works both from source and from the .exe)."""
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, *parts)


def _find_tool(name, winget_glob):
    bundled = os.path.join(app_dir(), "bin", name + ".exe")
    if os.path.isfile(bundled):
        return bundled
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    hits = glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", winget_glob))
    return hits[0] if hits else None


FFMPEG = _find_tool("ffmpeg", os.path.join("Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"))
DENO = _find_tool("deno", os.path.join("DenoLand.Deno*", "deno.exe"))


def load_ytdlp():
    """Import yt-dlp, preferring a newer copy fetched by 'Update engine'."""
    if os.path.isfile(ENGINE_ZIP) and zipfile.is_zipfile(ENGINE_ZIP):
        sys.path.insert(0, ENGINE_ZIP)
        try:
            import yt_dlp  # noqa: F401
            return sys.modules["yt_dlp"], "updated"
        except Exception:
            sys.path.remove(ENGINE_ZIP)
            for m in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                del sys.modules[m]
    import yt_dlp
    return yt_dlp, "built-in"


yt_dlp, ENGINE_SOURCE = load_ytdlp()
ENGINE_VERSION = yt_dlp.version.__version__
from yt_dlp.postprocessor.metadataparser import MetadataParserPP  # noqa: E402


def update_engine():
    """Download the latest yt-dlp. Takes effect next time the app starts."""
    os.makedirs(os.path.dirname(ENGINE_ZIP), exist_ok=True)
    tmp = ENGINE_ZIP + ".part"
    req = urllib.request.Request(ENGINE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if not zipfile.is_zipfile(tmp):
        os.remove(tmp)
        raise RuntimeError("Downloaded file was not a valid yt-dlp build")
    os.replace(tmp, ENGINE_ZIP)
    with zipfile.ZipFile(ENGINE_ZIP) as z:
        m = re.search(r"__version__\s*=\s*'([^']+)'", z.read("yt_dlp/version.py").decode())
    return m.group(1) if m else "latest"


class Cancelled(KeyboardInterrupt):
    pass


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _clean_error(e):
    msg = str(e)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    msg = msg.replace("ERROR: ", "")
    msg = re.sub(r"^\[[^\]]+\] [\w-]+: ", "", msg)
    if "Sign in to confirm" in msg:
        return "YouTube asked for sign-in (age-restricted or rate-limited)"
    if "Private video" in msg:
        return "This video is private"
    if "Video unavailable" in msg:
        return "Video unavailable"
    return msg.split("\n")[0][:140]


def _base_opts():
    o = {
        "quiet": True,
        "no_warnings": True,
        "logger": _Quiet(),
        "noplaylist": True,
        "windowsfilenames": True,
        "remote_components": ["ejs:github"],
    }
    if FFMPEG:
        o["ffmpeg_location"] = os.path.dirname(FFMPEG)
    if DENO:
        o["js_runtimes"] = {"deno": {"path": DENO}}
    return o


def _yt_thumb(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else None


# ---------------------------------------------------------------- resolving

def resolve_youtube(url, playlist=True):
    """Turn a link into a list of {url, title, subtitle, thumb} items."""
    opts = _base_opts() | {"extract_flat": "in_playlist", "skip_download": True,
                           "noplaylist": not playlist}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(_clean_error(e)) from None
    if not info:
        raise RuntimeError("Nothing found at that link")
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        items = []
        for e in info.get("entries") or []:
            if not e:
                continue
            vid = e.get("id")
            link = e.get("url") or e.get("webpage_url")
            if link and not link.startswith("http") and vid:
                link = f"https://www.youtube.com/watch?v={vid}"
            thumbs = e.get("thumbnails") or []
            items.append({
                "url": link,
                "title": e.get("title") or vid or "Untitled",
                "subtitle": e.get("channel") or e.get("uploader") or info.get("title") or "",
                "thumb": _yt_thumb(vid) if "youtube" in (e.get("ie_key") or "youtube").lower()
                else (thumbs[-1]["url"] if thumbs else None),
            })
        if not items:
            raise RuntimeError("That playlist is empty or private")
        return items, info.get("title")
    vid = info.get("id")
    return [{
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or "Untitled",
        "subtitle": info.get("channel") or info.get("uploader") or "",
        "thumb": info.get("thumbnail") if info.get("extractor_key") != "Youtube" else _yt_thumb(vid),
    }], None


SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]{22})")


def is_spotify(url):
    return bool(SPOTIFY_RE.search(url))


def _spotify_embed(kind, sid):
    req = urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{sid}",
                                 headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Spotify didn't return track info (link may be private)")
    data = json.loads(m.group(1))
    try:
        return data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError):
        raise RuntimeError("That Spotify link isn't available") from None


def _spotify_image(ent):
    imgs = ((ent.get("visualIdentity") or {}).get("image")
            or (ent.get("coverArt") or {}).get("sources") or [])
    imgs = [i for i in imgs if i.get("url")]
    if not imgs:
        return None
    return max(imgs, key=lambda i: i.get("maxWidth") or i.get("width") or 0)["url"]


def resolve_spotify(url):
    m = SPOTIFY_RE.search(url)
    if not m:
        raise RuntimeError("That doesn't look like a Spotify track, album or playlist link")
    kind, sid = m.groups()
    ent = _spotify_embed(kind, sid)
    if kind == "track":
        artists = ", ".join(a["name"] for a in ent.get("artists") or [])
        return [{"title": ent.get("name") or ent.get("title"), "subtitle": artists,
                 "artist": artists, "duration": (ent.get("duration") or 0) / 1000,
                 "thumb": _spotify_image(ent), "track_id": sid, "album": None}], None
    cover = _spotify_image(ent)
    is_album = kind == "album"
    tracks = ent.get("trackList") or []
    items = []
    for n, t in enumerate(tracks, 1):
        tid = (t.get("uri") or "").rsplit(":", 1)[-1]
        items.append({"title": t.get("title"), "subtitle": t.get("subtitle") or "",
                      "artist": t.get("subtitle") or "", "duration": (t.get("duration") or 0) / 1000,
                      "thumb": cover if is_album else None, "track_id": tid,
                      "album": ent.get("name") if is_album else None,
                      "album_artist": ent.get("subtitle") if is_album else None,
                      "track_no": n if is_album else None,
                      "track_total": len(tracks) if is_album else None})
    if not items:
        raise RuntimeError("That Spotify list is empty")
    return items, ent.get("name")


def spotify_track(track_id):
    """Full info for one Spotify track: title, artists, release date, cover, length."""
    try:
        ent = _spotify_embed("track", track_id)
    except Exception:
        return None
    date = ((ent.get("releaseDate") or {}).get("isoString") or "")[:10] or None
    return {"title": ent.get("name") or ent.get("title"),
            "artists": ", ".join(a["name"] for a in ent.get("artists") or []),
            "date": date, "cover": _spotify_image(ent),
            "duration": (ent.get("duration") or 0) / 1000 or None}


def spotify_cover(track_id):
    t = spotify_track(track_id)
    return t and t["cover"]


# ---------------------------------------------------------------- matching

_BAD_WORDS = ["live", "cover", "remix", "sped up", "slowed", "nightcore", "8d", "karaoke",
              "instrumental", "reaction", "lyrics video", "1 hour", "10 hours", "loop", "bass boosted"]


def _score(entry, title, artist, duration):
    t = (entry.get("title") or "").lower()
    ch = (entry.get("channel") or entry.get("uploader") or "").lower()
    want = f"{title} {artist}".lower()
    score = 0.0
    d = entry.get("duration")
    if duration and d:
        diff = abs(d - duration)
        score -= diff * 1.5 if diff > 3 else 0
        if diff > 30:
            score -= 60
    for w in _BAD_WORDS:
        if w in t and w not in want:
            score -= 40
    if ch.endswith(" - topic"):
        score += 35
    if "official audio" in t or "audio" in t:
        score += 10
    first_artist = artist.split(",")[0].strip().lower()
    if first_artist and (first_artist in t or first_artist in ch):
        score += 15
    words = [w for w in re.findall(r"\w+", title.lower()) if len(w) > 1]
    if words:
        score += 25 * sum(w in t for w in words) / len(words)
    return score


def match_youtube(title, artist, duration):
    query = f"{artist} - {title}".strip(" -")
    opts = _base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch8:{query} audio", download=False)
    entries = [e for e in (res or {}).get("entries") or [] if e and e.get("id")]
    if not entries:
        raise RuntimeError("No match found on YouTube")
    best = max(entries, key=lambda e: _score(e, title, artist, duration))
    return f"https://www.youtube.com/watch?v={best['id']}"


# ---------------------------------------------------------------- downloading

def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip(" .")
    return s[:150] or "track"


def download(item, mode, options, outdir, on_progress, is_cancelled):
    """Download one item. Returns the saved file path.

    mode: 'mp3' | 'mp4' | 'spotify'.  on_progress(stage, fraction_or_None).
    """
    os.makedirs(outdir, exist_ok=True)
    if not FFMPEG:
        raise RuntimeError("ffmpeg is missing - reinstall Wavegrab")
    result = {"path": None}

    def hook(d):
        if is_cancelled():
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = d.get("downloaded_bytes", 0) / total if total else None
            info = d.get("info_dict") or {}
            stage = "Downloading"
            if mode == "mp4":
                stage = "Downloading audio" if info.get("vcodec") == "none" else "Downloading video"
            on_progress(stage, frac)
        elif d["status"] == "finished":
            on_progress("Processing", 1.0)

    def pp_hook(d):
        if is_cancelled():
            raise Cancelled()
        if d["status"] == "started":
            names = {"ExtractAudio": "Converting to MP3", "Merger": "Merging video + audio",
                     "VideoConvertor": "Converting to MP4", "EmbedThumbnail": "Adding cover art",
                     "Metadata": "Adding tags"}
            key = d.get("postprocessor") or ""
            on_progress(next((v for k, v in names.items() if k in key), "Processing"), None)
        fp = (d.get("info_dict") or {}).get("filepath")
        if fp:
            result["path"] = fp

    opts = _base_opts() | {"progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
                           "overwrites": False, "continuedl": True}

    to_mp3 = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
               "preferredquality": options.get("quality", "320")}]
    sp_info = None
    if mode == "spotify":
        on_progress("Reading song info", None)
        sp_info = spotify_track(item["track_id"]) if item.get("track_id") else None
        duration = (sp_info or {}).get("duration") or item.get("duration")
        on_progress("Finding on YouTube", None)
        url = match_youtube(item["title"], item.get("artist", ""), duration)
        name = safe_name(f"{item.get('artist', '')} - {item['title']}".strip(" -"))
        opts |= {"format": "bestaudio/best", "outtmpl": os.path.join(outdir, name + ".%(ext)s"),
                 "postprocessors": to_mp3}
    elif mode == "mp3":
        url = item["url"]
        opts |= {"format": "bestaudio/best", "outtmpl": os.path.join(outdir, "%(title)s.%(ext)s"),
                 "postprocessors": to_mp3}
    else:
        url = item["url"]
        res = options.get("resolution", "1080p")
        if res == "Best":
            sort = ["res", "fps", "vcodec:h264", "acodec:aac"]
        else:
            sort = [f"res:{res.rstrip('p')}", "vcodec:h264", "acodec:aac"]
        opts |= {"format": "bv*+ba/b", "format_sort": sort, "merge_output_format": "mp4",
                 "outtmpl": os.path.join(outdir, "%(title)s.%(ext)s"), "writethumbnail": True,
                 "postprocessors": [
                     # Windows misreads "20050424" as a year; write 2005-04-24 instead
                     {"key": "MetadataParser", "when": "pre_process",
                      "actions": [(MetadataParserPP.Actions.INTERPRET,
                                   "%(release_date,upload_date>%Y-%m-%d)s", "%(meta_date)s")]},
                     {"key": "FFmpegMetadata", "add_metadata": True},
                     {"key": "EmbedThumbnail"}]}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not result["path"] and info:
                p = ydl.prepare_filename(info)
                ext = "mp4" if mode == "mp4" else "mp3"
                result["path"] = os.path.splitext(p)[0] + "." + ext
    except Cancelled:
        raise
    except Exception as e:
        raise RuntimeError(_clean_error(e)) from None

    path = result["path"]
    if mode in ("mp3", "spotify") and path and os.path.isfile(path):
        on_progress("Tagging song info + art", None)
        try:
            tags = meta.for_spotify(item, sp_info) if mode == "spotify" else meta.for_youtube(info)
            meta.write_mp3(path, tags)
            if mode == "mp3" and tags.get("music") and tags.get("artist") and tags.get("title"):
                # "Artist - Song (Official Video) [4K].mp3" -> "Artist - Song.mp3"
                nice = os.path.join(outdir, safe_name(f"{tags['artist']} - {tags['title']}") + ".mp3")
                if nice != path and not os.path.exists(nice):
                    os.replace(path, nice)
                    path = nice
        except Exception:
            pass  # a missing tag shouldn't fail a finished download
    return path


def _fetch(url, limit=8_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit)


def fetch_bytes(url):
    return _fetch(url)
