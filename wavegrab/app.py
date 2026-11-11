"""Wavegrab - YouTube MP3 / MP4 and Spotify downloader (UI)."""
import ctypes
import io
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageOps

from wavegrab import engine as E

VERSION = "1.0.0"

# ------------------------------------------------------------------ look
BG = "#0D0D12"
SIDE = "#13131A"
CARD = "#1A1A23"
CARD2 = "#24242F"
LINE = "#2C2C39"
TEXT = "#EDEDF2"
MUTED = "#8B8B9C"
DIM = "#555564"
RED = "#FF5C5C"

MODES = {
    "mp3": {"name": "YouTube to MP3", "badge": "MP3", "accent": "#FF4D6D", "glyph": 0xE8D6,
            "sub": "Rip the audio from any video or playlist as a high-quality MP3.",
            "hint": "Paste a YouTube video or playlist link…"},
    "mp4": {"name": "YouTube to MP4", "badge": "MP4", "accent": "#8B5CF6", "glyph": 0xE714,
            "sub": "Save videos up to 4K as MP4s that play anywhere.",
            "hint": "Paste a YouTube video or playlist link…"},
    "spotify": {"name": "Spotify to MP3", "badge": "SPOTIFY", "accent": "#1ED760", "glyph": 0xE90B,
                "sub": "Songs, albums and playlists, matched and tagged with cover art.",
                "hint": "Paste a Spotify song, album or playlist link…"},
}

G_DOWNLOAD, G_SETTINGS, G_FOLDER, G_PASTE = 0xE896, 0xE713, 0xE838, 0xE77F
G_CLOSE, G_CHECK, G_RETRY, G_WARN, G_OPEN, G_TRASH = 0xE711, 0xE73E, 0xE72C, 0xE7BA, 0xE8A7, 0xE74D

SETTINGS_FILE = os.path.join(E.DATA_DIR, "settings.json")


def blend(fg, bg, a):
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(b[i] + (f[i] - b[i]) * a):02x}" for i in range(3))


def clip(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def known_folder(guid, fallback):
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        import uuid
        u = uuid.UUID(guid)
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), fallback)


MUSIC = known_folder("4BD8D571-6D19-48D3-BE97-422220080E43", "Music")
VIDEOS = known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC", "Videos")


def load_settings():
    s = {"folders": {"mp3": os.path.join(MUSIC, "Wavegrab"),
                     "spotify": os.path.join(MUSIC, "Wavegrab"),
                     "mp4": os.path.join(VIDEOS, "Wavegrab")},
         "quality": "320", "resolution": "1080p", "playlist": True, "mode": "mp3"}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        s["folders"].update(saved.pop("folders", {}))
        s.update(saved)
    except Exception:
        pass
    return s


# ------------------------------------------------------------------ icons
_ICON_FONT = next((p for p in (r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf")
                   if os.path.isfile(p)), None)
_glyphs = {}


def glyph_pil(cp, color, px):
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    if not _ICON_FONT:
        return img
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(_ICON_FONT, int(px * 0.84))
    x0, y0, x1, y1 = d.textbbox((0, 0), chr(cp), font=f)
    d.text(((px - (x1 - x0)) / 2 - x0, (px - (y1 - y0)) / 2 - y0), chr(cp), font=f, fill=color)
    return img


def glyph(cp, color, size=18):
    key = (cp, color, size)
    if key not in _glyphs:
        img = glyph_pil(cp, color, size * 3).resize((size * 2, size * 2), Image.LANCZOS)
        _glyphs[key] = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    return _glyphs[key]


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius=radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def tile(color, glyph_cp, glyph_color, w, h, radius):
    """Rounded colour tile with a centred glyph (2x resolution)."""
    im = Image.new("RGBA", (w * 2, h * 2), color)
    g = glyph_pil(glyph_cp, glyph_color, int(min(w, h) * 1.1))
    im.alpha_composite(g, ((w * 2 - g.width) // 2, (h * 2 - g.height) // 2))
    return rounded(im, radius * 2)


# ------------------------------------------------------------------ queue card
class Card(ctk.CTkFrame):
    def __init__(self, app, parent, item):
        super().__init__(parent, fg_color=CARD, corner_radius=14)
        self.app, self.item = app, item
        m = MODES[item["mode"]]
        self.accent = m["accent"]
        self.tw, self.th = (54, 54) if item["mode"] == "spotify" else (96, 54)
        self.grid_columnconfigure(1, weight=1)

        ph = tile(CARD2, m["glyph"], DIM, self.tw, self.th, 8)
        self.thumb = ctk.CTkLabel(self, text="", width=self.tw, height=self.th,
                                  image=ctk.CTkImage(ph, ph, size=(self.tw, self.th)))
        self.thumb.grid(row=0, column=0, rowspan=3, padx=(12, 14), pady=12)

        self.title = ctk.CTkLabel(self, text=clip(item["title"], 70), font=app.f(14, "bold"),
                                  text_color=TEXT, anchor="w", height=20)
        self.title.grid(row=0, column=1, sticky="sw", pady=(12, 0))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=1, sticky="w", pady=(2, 0))
        ctk.CTkLabel(row, text=f" {m['badge']} ", font=app.f(9, "bold"), height=18,
                     fg_color=blend(self.accent, CARD, 0.16), text_color=self.accent,
                     corner_radius=6).pack(side="left", padx=(0, 8))
        self.sub = ctk.CTkLabel(row, text=clip(item.get("subtitle"), 60), font=app.f(12),
                                text_color=MUTED, height=18)
        self.sub.pack(side="left")

        self.bar = ctk.CTkProgressBar(self, height=4, corner_radius=2, fg_color=CARD2,
                                      progress_color=self.accent)
        self.bar.set(0)
        self.bar.grid(row=2, column=1, sticky="ew", pady=(8, 12))

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=3, padx=(14, 10), sticky="e")
        self.status = ctk.CTkLabel(right, text="", font=app.f(12), text_color=MUTED,
                                   width=150, anchor="e")
        self.status.pack(side="left", padx=(0, 6))
        self.actions = ctk.CTkFrame(right, fg_color="transparent")
        self.actions.pack(side="left")
        self.set_state(item["state"], item.get("status", "Waiting"))

    def icon_btn(self, cp, cmd, tip_color=MUTED):
        b = ctk.CTkButton(self.actions, text="", image=glyph(cp, tip_color, 15), width=32, height=32,
                          fg_color="transparent", hover_color=CARD2, corner_radius=8, command=cmd)
        b.pack(side="left", padx=1)
        return b

    def set_thumb(self, pil):
        self.thumb.configure(image=ctk.CTkImage(pil, pil, size=(self.tw, self.th)))

    def set_progress(self, stage, frac):
        if frac is None:
            self.status.configure(text=stage + "…")
        else:
            self.bar.set(frac)
            self.status.configure(text=f"{stage}  {int(frac * 100)}%")

    def set_state(self, state, text):
        for w in self.actions.winfo_children():
            w.destroy()
        iid = self.item["id"]
        self.bar.configure(progress_color=self.accent)
        if state == "resolving":
            self.bar.configure(mode="indeterminate")
            self.bar.start()
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate")
        color = MUTED
        if state in ("queued", "resolving"):
            self.bar.set(0)
            if state == "queued":
                self.bar.configure(progress_color=CARD2)  # no stray accent dot on an empty bar
            self.icon_btn(G_CLOSE, lambda: self.app.remove(iid))
        elif state == "active":
            self.icon_btn(G_CLOSE, lambda: self.app.cancel(iid))
        elif state == "done":
            self.bar.set(1)
            color = self.accent
            text = "✓  " + text
            self.icon_btn(G_FOLDER, lambda: self.app.reveal(iid))
            self.icon_btn(G_CLOSE, lambda: self.app.remove(iid))
        elif state in ("error", "cancelled"):
            self.bar.set(1 if state == "error" else 0)
            self.bar.configure(progress_color=RED if state == "error" else DIM)
            color = RED if state == "error" else MUTED
            if state == "error":
                self.sub.configure(text=clip(text, 70), text_color=RED)
                text = "Failed"
            if self.item.get("data"):
                self.icon_btn(G_RETRY, lambda: self.app.retry(iid))
            self.icon_btn(G_CLOSE, lambda: self.app.remove(iid))
        self.status.configure(text=text, text_color=color)


# ------------------------------------------------------------------ app
class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.settings = load_settings()
        self.mode = self.settings.get("mode", "mp3") if self.settings.get("mode") in MODES else "mp3"
        self.items, self.cards = {}, {}
        self.ids = itertools.count(1)
        self.ui_q = queue.Queue()
        self.jobs = queue.Queue()
        self.thumbs = ThreadPoolExecutor(4)
        self._fonts = {}
        self._toast_after = None

        self.title("Wavegrab")
        self.geometry("1080x720")
        self.minsize(920, 600)
        try:
            self.iconbitmap(E.resource("assets", "icon.ico"))
        except Exception:
            pass
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.build_sidebar()
        self.build_download_page()
        self.build_settings_page()
        self.show_page("download")
        self.set_mode(self.mode)

        for _ in range(2):
            threading.Thread(target=self.worker, daemon=True).start()
        self.after(60, self.pump)
        self.after(80, self.style_titlebar)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if not E.FFMPEG:
            self.after(600, lambda: self.toast("ffmpeg is missing — downloads won't work", RED, 8000))

    # ---------- helpers
    def f(self, size, weight="normal"):
        key = (size, weight)
        if key not in self._fonts:
            self._fonts[key] = ctk.CTkFont(family="Segoe UI", size=size, weight=weight)
        return self._fonts[key]

    def style_titlebar(self):
        """Dark Windows 11 title bar that blends into the app."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
            r, g, b = (int(SIDE[i:i + 2], 16) for i in (1, 3, 5))
            color = ctypes.c_int(r | (g << 8) | (b << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)
        except Exception:
            pass

    def save_settings(self):
        self.settings["mode"] = self.mode
        try:
            os.makedirs(E.DATA_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    def post(self, *msg):
        self.ui_q.put(msg)

    # ---------- sidebar
    def build_sidebar(self):
        side = ctk.CTkFrame(self, fg_color=SIDE, corner_radius=0, width=236)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(6, weight=1)

        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="w", padx=22, pady=(26, 30))
        try:
            ic = Image.open(E.resource("assets", "icon.png"))
            ctk.CTkLabel(logo, text="", image=ctk.CTkImage(ic, ic, size=(34, 34))).pack(side="left")
        except Exception:
            pass
        ctk.CTkLabel(logo, text="Wavegrab", font=self.f(21, "bold"), text_color=TEXT).pack(side="left", padx=(10, 0))

        ctk.CTkLabel(side, text="DOWNLOAD", font=self.f(10, "bold"), text_color=DIM).grid(
            row=1, column=0, sticky="w", padx=26, pady=(0, 6))
        self.nav = {}
        for r, key in enumerate(MODES, start=2):
            b = ctk.CTkButton(side, text="  " + MODES[key]["name"], anchor="w", height=44, corner_radius=10,
                              font=self.f(14, "bold"), fg_color="transparent", hover_color=CARD,
                              text_color=MUTED, image=glyph(MODES[key]["glyph"], MUTED, 18),
                              command=lambda k=key: (self.show_page("download"), self.set_mode(k)))
            b.grid(row=r, column=0, sticky="ew", padx=14, pady=2)
            self.nav[key] = b

        self.nav["settings"] = ctk.CTkButton(
            side, text="  Settings", anchor="w", height=44, corner_radius=10, font=self.f(14, "bold"),
            fg_color="transparent", hover_color=CARD, text_color=MUTED, image=glyph(G_SETTINGS, MUTED, 18),
            command=lambda: self.show_page("settings"))
        self.nav["settings"].grid(row=7, column=0, sticky="ew", padx=14, pady=(0, 6))
        ctk.CTkLabel(side, text=f"v{VERSION}  ·  engine {E.ENGINE_VERSION}", font=self.f(10),
                     text_color=DIM).grid(row=8, column=0, sticky="w", padx=26, pady=(0, 18))

    def highlight_nav(self, key):
        for k, b in self.nav.items():
            cp = MODES[k]["glyph"] if k in MODES else G_SETTINGS
            accent = MODES[k]["accent"] if k in MODES else TEXT
            if k == key:
                b.configure(fg_color=blend(accent, SIDE, 0.14), hover_color=blend(accent, SIDE, 0.2),
                            text_color=TEXT, image=glyph(cp, accent, 18))
            else:
                b.configure(fg_color="transparent", hover_color=CARD, text_color=MUTED,
                            image=glyph(cp, MUTED, 18))

    def show_page(self, page):
        self.page = page
        if page == "download":
            self.settings_page.grid_remove()
            self.dl_page.grid()
            self.highlight_nav(self.mode)
        else:
            self.dl_page.grid_remove()
            self.refresh_settings()
            self.settings_page.grid()
            self.highlight_nav("settings")

    # ---------- download page
    def build_download_page(self):
        p = self.dl_page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        p.grid(row=0, column=1, sticky="nsew")
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(4, weight=1)

        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=36, pady=(32, 20))
        self.badge = ctk.CTkLabel(head, text="", width=52, height=52)
        self.badge.pack(side="left")
        txt = ctk.CTkFrame(head, fg_color="transparent")
        txt.pack(side="left", padx=(16, 0))
        self.h_title = ctk.CTkLabel(txt, text="", font=self.f(26, "bold"), text_color=TEXT, anchor="w", height=34)
        self.h_title.pack(anchor="w")
        self.h_sub = ctk.CTkLabel(txt, text="", font=self.f(13), text_color=MUTED, anchor="w", height=20)
        self.h_sub.pack(anchor="w")

        box = ctk.CTkFrame(p, fg_color=CARD, corner_radius=18)
        box.grid(row=1, column=0, sticky="ew", padx=36)
        box.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(box, height=50, corner_radius=12, border_width=1, border_color=LINE,
                                  fg_color=BG, text_color=TEXT, placeholder_text_color=DIM, font=self.f(14))
        self.entry.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=(16, 12))
        self.entry.bind("<Return>", lambda e: self.grab())
        self.paste_btn = ctk.CTkButton(box, text="Paste", width=96, height=50, corner_radius=12,
                                       font=self.f(13, "bold"), fg_color=CARD2, hover_color=LINE,
                                       text_color=TEXT, image=glyph(G_PASTE, TEXT, 16), command=self.paste)
        self.paste_btn.grid(row=0, column=1, padx=4, pady=(16, 12))
        self.go_btn = ctk.CTkButton(box, text="Download", width=148, height=50, corner_radius=12,
                                    font=self.f(14, "bold"), command=self.grab)
        self.go_btn.grid(row=0, column=2, padx=(4, 16), pady=(16, 12))

        opts = ctk.CTkFrame(box, fg_color="transparent")
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 16))
        self.opt_label = ctk.CTkLabel(opts, text="", font=self.f(12, "bold"), text_color=MUTED)
        self.opt_label.pack(side="left", padx=(2, 10))
        self.seg = ctk.CTkSegmentedButton(opts, height=32, corner_radius=9, font=self.f(12, "bold"),
                                          fg_color=CARD2, unselected_color=CARD2, unselected_hover_color=LINE,
                                          text_color=TEXT, command=self.on_seg)
        self.seg.pack(side="left")
        self.pl_var = ctk.BooleanVar(value=self.settings.get("playlist", True))
        self.pl_switch = ctk.CTkSwitch(opts, text="Whole playlists", variable=self.pl_var, font=self.f(12, "bold"),
                                       text_color=MUTED, fg_color=CARD2, button_color=TEXT,
                                       button_hover_color="#ffffff", command=self.on_playlist)
        self.pl_switch.pack(side="left", padx=(22, 0))
        self.folder_btn = ctk.CTkButton(opts, text="", height=32, corner_radius=9, font=self.f(12),
                                        fg_color=CARD2, hover_color=LINE, text_color=MUTED,
                                        image=glyph(G_FOLDER, MUTED, 15), command=self.pick_folder)
        self.folder_btn.pack(side="right")

        qh = ctk.CTkFrame(p, fg_color="transparent")
        qh.grid(row=2, column=0, sticky="ew", padx=40, pady=(26, 8))
        ctk.CTkLabel(qh, text="Queue", font=self.f(17, "bold"), text_color=TEXT).pack(side="left")
        self.count = ctk.CTkLabel(qh, text="", font=self.f(12), text_color=MUTED)
        self.count.pack(side="left", padx=12)
        ctk.CTkButton(qh, text="Open folder", width=10, height=30, corner_radius=8, font=self.f(12, "bold"),
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      image=glyph(G_OPEN, MUTED, 14), command=self.open_folder).pack(side="right")
        ctk.CTkButton(qh, text="Clear finished", width=10, height=30, corner_radius=8, font=self.f(12, "bold"),
                      fg_color="transparent", hover_color=CARD, text_color=MUTED,
                      image=glyph(G_TRASH, MUTED, 14), command=self.clear_finished).pack(side="right", padx=4)

        self.queue_frame = ctk.CTkScrollableFrame(p, fg_color="transparent", corner_radius=0,
                                                  scrollbar_button_color=CARD2,
                                                  scrollbar_button_hover_color=LINE)
        self.queue_frame.grid(row=4, column=0, sticky="nsew", padx=(28, 22), pady=(0, 20))
        self.queue_frame.grid_columnconfigure(0, weight=1)

        self.empty = ctk.CTkFrame(p, fg_color="transparent")
        ctk.CTkLabel(self.empty, text="", image=glyph(G_DOWNLOAD, DIM, 46)).pack(pady=(0, 10))
        ctk.CTkLabel(self.empty, text="Nothing in the queue yet", font=self.f(16, "bold"), text_color=MUTED).pack()
        self.empty_hint = ctk.CTkLabel(self.empty, text="", font=self.f(13), text_color=DIM)
        self.empty_hint.pack(pady=(4, 0))

        self.toast_lbl = ctk.CTkLabel(p, text="", font=self.f(13, "bold"), fg_color=CARD2, text_color=TEXT,
                                      corner_radius=10, height=40)
        self.update_empty()

    def set_mode(self, mode):
        self.mode = mode
        m = MODES[mode]
        a = m["accent"]
        self.badge.configure(image=ctk.CTkImage(*(2 * [tile(blend(a, BG, 0.16), m["glyph"], a, 52, 52, 14)]),
                                                size=(52, 52)))
        self.h_title.configure(text=m["name"])
        self.h_sub.configure(text=m["sub"])
        dark_text = mode == "spotify"
        self.go_btn.configure(fg_color=a, hover_color=blend(a, "#ffffff", 0.15),
                              text_color=BG if dark_text else "#ffffff",
                              image=glyph(G_DOWNLOAD, BG if dark_text else "#ffffff", 17))
        self.entry.configure(placeholder_text=m["hint"])
        self.entry.focus_set() if not self.entry.get() else None
        if mode == "mp4":
            self.opt_label.configure(text="Resolution")
            self.seg.configure(values=["480p", "720p", "1080p", "Best"])
            self.seg.set(self.settings.get("resolution", "1080p"))
        else:
            self.opt_label.configure(text="Quality")
            self.seg.configure(values=["128", "192", "256", "320"])
            self.seg.set(self.settings.get("quality", "320"))
        self.seg.configure(selected_color=a, selected_hover_color=blend(a, "#ffffff", 0.15),
                           text_color=TEXT)
        self.pl_switch.configure(progress_color=a)
        if mode == "spotify":
            self.pl_switch.pack_forget()
        elif not self.pl_switch.winfo_ismapped():
            self.pl_switch.pack(side="left", padx=(22, 0), after=self.seg)
        self.update_folder_btn()
        self.empty_hint.configure(text="Paste a link above and press Download — or just hit Enter.")
        if self.page == "download":
            self.highlight_nav(mode)
        self.save_settings()

    def on_seg(self, value):
        self.settings["resolution" if self.mode == "mp4" else "quality"] = value
        self.save_settings()

    def on_playlist(self):
        self.settings["playlist"] = bool(self.pl_var.get())
        self.save_settings()

    def update_folder_btn(self):
        path = self.settings["folders"][self.mode]
        short = path if len(path) < 40 else "…" + path[-38:]
        self.folder_btn.configure(text="  " + short)

    def pick_folder(self, mode=None):
        mode = mode or self.mode
        d = filedialog.askdirectory(initialdir=self.settings["folders"][mode], title="Choose where to save")
        if d:
            self.settings["folders"][mode] = os.path.normpath(d)
            self.save_settings()
            self.update_folder_btn()
            self.refresh_settings()

    def open_folder(self):
        d = self.settings["folders"][self.mode]
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def paste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            text = ""
        if not text:
            self.toast("Clipboard is empty")
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
        self.grab()

    def toast(self, text, color=TEXT, ms=3200):
        self.toast_lbl.configure(text="   " + text + "   ", text_color=color)
        self.toast_lbl.place(relx=0.5, rely=1.0, y=-26, anchor="s")
        self.toast_lbl.lift()
        if self._toast_after:
            self.after_cancel(self._toast_after)
        self._toast_after = self.after(ms, self.toast_lbl.place_forget)

    # ---------- queue management
    def update_empty(self):
        live = [i for i in self.items.values()]
        if live:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.64, anchor="center")
        done = sum(i["state"] == "done" for i in live)
        busy = sum(i["state"] in ("queued", "active", "resolving") for i in live)
        if not live:
            self.count.configure(text="")
        elif busy:
            self.count.configure(text=f"{done} of {len(live)} done")
        else:
            self.count.configure(text=f"{len(live)} item{'s' if len(live) != 1 else ''}")

    def add_card(self, item, before=None):
        """before= keeps cards in the order links were added, even if a later link resolves first."""
        self.items[item["id"]] = item
        card = Card(self, self.queue_frame, item)
        if before is not None:
            card.pack(fill="x", padx=8, pady=5, before=before)
        else:
            card.pack(fill="x", padx=8, pady=5)
        self.cards[item["id"]] = card
        self.update_empty()
        return card

    def grab(self):
        url = self.entry.get().strip()
        if not url:
            self.toast("Paste a link first")
            return
        mode = self.mode
        if E.is_spotify(url) and mode != "spotify":
            mode = "spotify"
            self.set_mode(mode)
            self.toast("Spotify link spotted — switched to Spotify mode", MODES[mode]["accent"])
        elif mode == "spotify" and not E.is_spotify(url):
            if "spotify.com" in url:
                self.toast("Only Spotify songs, albums and playlists are supported", RED)
                return
            mode = "mp3"
            self.set_mode(mode)
            self.toast("YouTube link spotted — switched to YouTube to MP3", MODES[mode]["accent"])
        if not url.lower().startswith(("http", "spotify:", "www.", "youtu", "m.youtube")):
            self.toast("That doesn't look like a link", RED)
            return
        self.entry.delete(0, "end")
        opts = {"quality": self.settings.get("quality", "320"),
                "resolution": self.settings.get("resolution", "1080p")}
        temp = {"id": next(self.ids), "mode": mode, "title": url, "subtitle": "Looking up link",
                "state": "resolving", "status": "Looking up…"}
        self.add_card(temp)
        threading.Thread(target=self.resolve, daemon=True,
                         args=(temp["id"], url, mode, opts, self.settings["folders"][mode],
                               bool(self.pl_var.get()))).start()

    def resolve(self, temp_id, url, mode, opts, outdir, playlist):
        try:
            if mode == "spotify":
                found, _ = E.resolve_spotify(url)
            else:
                found, _ = E.resolve_youtube(url, playlist)
            self.post("resolved", temp_id, found, mode, opts, outdir)
        except Exception as e:
            self.post("state", temp_id, "error", str(e) or "Couldn't read that link")

    def on_resolved(self, temp_id, found, mode, opts, outdir):
        if temp_id not in self.items:
            return
        placeholder = self.cards.get(temp_id)
        for data in found:
            item = {"id": next(self.ids), "mode": mode, "title": data["title"], "subtitle": data.get("subtitle"),
                    "data": data, "opts": opts, "outdir": outdir, "state": "queued", "status": "Waiting",
                    "cancel": False}
            card = self.add_card(item, before=placeholder)
            self.thumbs.submit(self.load_thumb, item["id"], data, card.tw, card.th)
            self.jobs.put(item["id"])
        self.remove(temp_id)
        if len(found) > 1:
            self.toast(f"Added {len(found)} items to the queue", MODES[mode]["accent"])

    def load_thumb(self, iid, data, w, h):
        try:
            url = data.get("thumb")
            if not url and data.get("track_id"):
                url = E.spotify_cover(data["track_id"])
                data["thumb"] = url
            if not url:
                return
            im = Image.open(io.BytesIO(E.fetch_bytes(url))).convert("RGB")
            im = rounded(ImageOps.fit(im, (w * 2, h * 2), Image.LANCZOS), 16)
            self.post("thumb", iid, im)
        except Exception:
            pass

    def remove(self, iid):
        item = self.items.pop(iid, None)
        if item:
            item["cancel"] = True
            item["state"] = "removed"
        card = self.cards.pop(iid, None)
        if card:
            card.destroy()
        self.update_empty()

    def cancel(self, iid):
        if iid in self.items:
            self.items[iid]["cancel"] = True
            self.cards[iid].status.configure(text="Stopping…")

    def retry(self, iid):
        item = self.items.get(iid)
        if not item:
            return
        item.update(state="queued", cancel=False)
        card = self.cards[iid]
        card.sub.configure(text=clip(item.get("subtitle"), 60), text_color=MUTED)
        card.set_state("queued", "Waiting")
        self.jobs.put(iid)
        self.update_empty()

    def reveal(self, iid):
        path = self.items.get(iid, {}).get("path")
        if path and os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            self.open_folder()

    def clear_finished(self):
        for iid, it in list(self.items.items()):
            if it["state"] in ("done", "error", "cancelled"):
                self.remove(iid)

    # ---------- background download workers
    def worker(self):
        while True:
            iid = self.jobs.get()
            item = self.items.get(iid)
            if not item or item["state"] != "queued":
                continue
            item["state"] = "active"
            self.post("state", iid, "active", "Starting…")
            last = [None, None]

            def progress(stage, frac, iid=iid):
                pct = None if frac is None else int(frac * 100)
                if [stage, pct] != last:
                    last[:] = [stage, pct]
                    self.post("progress", iid, stage, frac)

            try:
                path = E.download(item["data"], item["mode"], item["opts"], item["outdir"],
                                  progress, lambda: item["cancel"])
                item["path"] = path
                self.post("state", iid, "done", "Saved")
            except E.Cancelled:
                self.post("state", iid, "cancelled", "Cancelled")
            except Exception as e:
                self.post("state", iid, "error", str(e) or "Download failed")

    def pump(self):
        try:
            for _ in range(200):
                msg = self.ui_q.get_nowait()
                kind, iid = msg[0], msg[1]
                if kind == "resolved":
                    self.on_resolved(*msg[1:])
                    continue
                card, item = self.cards.get(iid), self.items.get(iid)
                if not card or not item:
                    continue
                if kind == "progress":
                    card.set_progress(msg[2], msg[3])
                elif kind == "thumb":
                    card.set_thumb(msg[2])
                elif kind == "state":
                    item["state"] = msg[2]
                    card.set_state(msg[2], msg[3])
                    self.update_empty()
                    if msg[2] in ("done", "error", "cancelled"):
                        self.check_all_done()
        except queue.Empty:
            pass
        self.after(60, self.pump)

    def check_all_done(self):
        states = [i["state"] for i in self.items.values()]
        if states and not any(s in ("queued", "active", "resolving") for s in states):
            ok = states.count("done")
            bad = states.count("error")
            msg = f"All done — {ok} saved" + (f", {bad} failed" if bad else "")
            self.toast(msg, RED if bad and not ok else MODES[self.mode]["accent"], 5000)

    # ---------- settings page
    def build_settings_page(self):
        p = self.settings_page = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0,
                                                         scrollbar_button_color=CARD2)
        p.grid(row=0, column=1, sticky="nsew")
        p.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(p, text="Settings", font=self.f(26, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=36, pady=(32, 20))

        def section(row, title):
            ctk.CTkLabel(p, text=title.upper(), font=self.f(11, "bold"), text_color=DIM).grid(
                row=row, column=0, sticky="w", padx=40, pady=(10, 6))
            box = ctk.CTkFrame(p, fg_color=CARD, corner_radius=16)
            box.grid(row=row + 1, column=0, sticky="ew", padx=36, pady=(0, 18))
            box.grid_columnconfigure(1, weight=1)
            return box

        box = section(1, "Save locations")
        self.folder_labels = {}
        for r, key in enumerate(MODES):
            m = MODES[key]
            ctk.CTkLabel(box, text="", image=ctk.CTkImage(*(2 * [tile(blend(m["accent"], CARD, 0.16), m["glyph"],
                                                                       m["accent"], 36, 36, 10)]), size=(36, 36))
                         ).grid(row=r, column=0, padx=(18, 12), pady=10)
            t = ctk.CTkFrame(box, fg_color="transparent")
            t.grid(row=r, column=1, sticky="w")
            ctk.CTkLabel(t, text=m["name"], font=self.f(13, "bold"), text_color=TEXT, height=18).pack(anchor="w")
            self.folder_labels[key] = ctk.CTkLabel(t, text="", font=self.f(12), text_color=MUTED, height=18)
            self.folder_labels[key].pack(anchor="w")
            ctk.CTkButton(box, text="Change", width=90, height=34, corner_radius=9, font=self.f(12, "bold"),
                          fg_color=CARD2, hover_color=LINE, text_color=TEXT,
                          command=lambda k=key: self.pick_folder(k)).grid(row=r, column=2, padx=18)

        box = section(3, "Download engine")
        ctk.CTkLabel(box, text="", image=glyph(G_RETRY, TEXT, 22)).grid(row=0, column=0, padx=(22, 14), pady=18)
        t = ctk.CTkFrame(box, fg_color="transparent")
        t.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(t, text=f"yt-dlp {E.ENGINE_VERSION}  ({E.ENGINE_SOURCE})", font=self.f(13, "bold"),
                     text_color=TEXT, height=18).pack(anchor="w")
        self.engine_lbl = ctk.CTkLabel(t, text="If downloads start failing, YouTube probably changed something. "
                                               "Updating usually fixes it.", font=self.f(12), text_color=MUTED,
                                       height=18, wraplength=520, justify="left")
        self.engine_lbl.pack(anchor="w")
        self.update_btn = ctk.CTkButton(box, text="Update", width=90, height=34, corner_radius=9,
                                        font=self.f(12, "bold"), fg_color=CARD2, hover_color=LINE,
                                        text_color=TEXT, command=self.update_engine)
        self.update_btn.grid(row=0, column=2, padx=18)

        box = section(5, "About")
        ctk.CTkLabel(box, text="", image=ctk.CTkImage(*(2 * [Image.open(E.resource("assets", "icon.png"))]),
                                                      size=(44, 44))).grid(row=0, column=0, padx=(18, 14), pady=18)
        t = ctk.CTkFrame(box, fg_color="transparent")
        t.grid(row=0, column=1, sticky="w", pady=14)
        ctk.CTkLabel(t, text=f"Wavegrab {VERSION}", font=self.f(14, "bold"), text_color=TEXT, height=20).pack(anchor="w")
        ctk.CTkLabel(t, text="Built on yt-dlp and FFmpeg. Spotify tracks are matched to the same song on YouTube.\n"
                             "Only download content you have the right to.",
                     font=self.f(12), text_color=MUTED, justify="left").pack(anchor="w")
        self.settings_page.grid_remove()

    def refresh_settings(self):
        for key, lbl in getattr(self, "folder_labels", {}).items():
            path = self.settings["folders"][key]
            lbl.configure(text=path if len(path) <= 64 else "…" + path[-63:])

    def update_engine(self):
        self.update_btn.configure(state="disabled", text="Updating…")

        def run():
            try:
                v = E.update_engine()
                msg = (f"Got yt-dlp {v}. Restart Wavegrab to use it.", "#1ED760")
            except Exception as e:
                msg = (f"Update failed: {e}", RED)
            self.after(0, lambda: (self.engine_lbl.configure(text=msg[0], text_color=msg[1]),
                                   self.update_btn.configure(state="normal", text="Update")))

        threading.Thread(target=run, daemon=True).start()

    def on_close(self):
        self.save_settings()
        for it in self.items.values():
            it["cancel"] = True
        self.destroy()
        os._exit(0)


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Wavegrab.App")
    except Exception:
        pass
    ctk.set_appearance_mode("dark")
    App().mainloop()


if __name__ == "__main__":
    main()
