#!/usr/bin/env python3
"""Screens -> ChatGPT: capture selected monitors and paste them into the
ChatGPT desktop app. Dark UI, live monitor previews, prompt presets, and a
global Ctrl+Alt+G hotkey."""

import ctypes
from ctypes import wintypes
import datetime
import io
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import winsound

import mss
import mss.tools
import pyautogui
import pygetwindow as gw
import win32clipboard
import win32con
import win32gui
from PIL import Image, ImageTk

DEFAULT_PROMPT = (
    "Here are my ATAS trading charts. What do you see - any notable "
    "setups, levels, or order flow worth flagging?"
)
PRESETS = {
    "Order flow read": (
        "Read the order flow on my ATAS charts - notable setups, absorption, "
        "exhaustion, delta divergences, and the key levels that matter right now."
    ),
    "Key levels": (
        "Mark out the key support/resistance and high-volume levels visible "
        "on my charts, with rough prices for each."
    ),
    "Bias check": (
        "Based on these charts, what's the current bias - long, short, or "
        "stay flat? One tight paragraph of reasoning."
    ),
    "Position risk": (
        "I'm in a position. What on these charts should worry me? "
        "Invalidation levels and warning signs, briefly."
    ),
    "Describe screens": DEFAULT_PROMPT,
}

WINDOW_TITLE_HINT = "ChatGPT"
# The real desktop app's process - matching on window title alone is not
# enough, because OTHER windows can have "ChatGPT" in their title (including
# this tool's own window), and pasting would silently go to the wrong one.
CHATGPT_EXE_NAMES = {"chatgpt.exe", "chatgpt classic.exe"}
PASTE_DELAY = 1.2  # seconds to let each image attach/render before the next paste
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
SAVE_DIR = os.path.join(APP_DIR, "screenshots")
CONFIG_PATH = os.path.join(APP_DIR, "screens_to_chatgpt_config.json")


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class ConfigError(Exception):
    pass


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        # utf-8-sig so a byte-order mark (which some editors and PowerShell
        # add) doesn't make json.load choke.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as exc:
        # Never silently fall back to defaults: saving would then overwrite
        # the user's saved prompts with an empty set. Keep the bad file so
        # it can be recovered by hand.
        backup = CONFIG_PATH + ".broken"
        try:
            os.replace(CONFIG_PATH, backup)
        except Exception:
            backup = None
        raise ConfigError(
            f"Couldn't read your settings ({exc}). "
            + (f"The old file was kept as {os.path.basename(backup)}."
               if backup else "")
        )


def save_config(cfg):
    # Write to a temp file then swap it in, so an interrupted write can't
    # leave a half-written config behind.
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass

HOTKEY_MODS = 0x0002 | 0x0001  # MOD_CONTROL | MOD_ALT
HOTKEY_VK = ord("G")
HOTKEY_LABEL = "Ctrl+Alt+G"

# Palette
BG = "#0e1218"
PANEL = "#161c26"
PANEL_HI = "#1d2430"
TEXT = "#e8eaed"
MUTED = "#8a93a3"
ACCENT = "#10a37f"      # ChatGPT green
ACCENT_DIM = "#0d8a6a"
DANGER = "#e5534b"
BORDER_OFF = "#2a3140"

THUMB_W, THUMB_H = 148, 84


def _hwnd_pid(hwnd):
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _pid_exe_name(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        )
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def find_chatgpt_window():
    own_pid = os.getpid()
    fallback = None
    for w in gw.getAllWindows():
        title = w.title.strip()
        if not title or WINDOW_TITLE_HINT.lower() not in title.lower():
            continue
        hwnd = w._hWnd
        pid = _hwnd_pid(hwnd)
        if pid == own_pid:
            continue
        if _pid_exe_name(pid) in CHATGPT_EXE_NAMES:
            return hwnd
        if fallback is None and title.lower() in ("chatgpt", "chatgpt classic"):
            fallback = hwnd
    return fallback


def list_monitors():
    with mss.mss() as sct:
        return list(sct.monitors[1:])


def grab_thumbnails():
    """One (normal, dimmed) PIL image pair per monitor, thumbnail-sized."""
    pairs = []
    with mss.mss() as sct:
        for mon in sct.monitors[1:]:
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            img.thumbnail((THUMB_W, THUMB_H))
            dimmed = img.point(lambda p: int(p * 0.35))
            pairs.append((img, dimmed))
    return pairs


def capture_screens(selected_indices):
    os.makedirs(SAVE_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = []
    with mss.mss() as sct:
        for i, monitor in enumerate(sct.monitors[1:]):
            if i not in selected_indices:
                continue
            shot = sct.grab(monitor)
            path = os.path.join(SAVE_DIR, f"screen_{timestamp}_{i + 1}.png")
            mss.tools.to_png(shot.rgb, shot.size, output=path)
            paths.append(path)
    return paths


def set_clipboard_text(text):
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    win32clipboard.CloseClipboard()


def set_clipboard_image(path):
    image = Image.open(path)
    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # strip the 14-byte BMP file header -> raw DIB
    output.close()
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()


def focus_and_click_composer(hwnd, new_chat=False):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    for _attempt in range(2):
        # Windows silently blocks a background process from stealing
        # foreground focus. Faking an Alt keypress first tricks it into
        # allowing the switch - without this, SetForegroundWindow can
        # appear to succeed but do nothing.
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0x12, 0, 0x2, 0)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(1.0)
        if win32gui.GetForegroundWindow() == hwnd:
            break
    else:
        return False

    if new_chat:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        pyautogui.click(left + width * 0.06, top + height * 0.083)
        time.sleep(0.8)

    # Being the OS foreground window is not the same as having keyboard
    # focus INSIDE an Electron app's text field. Shift+Esc is ChatGPT's own
    # "focus chat input" shortcut - unlike clicking a fixed spot, it works
    # wherever the composer sits (centred in an empty chat, pinned to the
    # bottom in one with history), so captures land in the open conversation.
    pyautogui.hotkey("shift", "esc")
    time.sleep(0.5)

    # Clear any leftover text so a capture that was staged but never sent
    # doesn't get this one's prompt appended to it. (Stale image
    # attachments can't be cleared this way - they stay visible for review.)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    time.sleep(0.2)

    return win32gui.GetForegroundWindow() == hwnd


def enable_dark_titlebar(hwnd):
    try:
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value), ctypes.sizeof(value),
        )
    except Exception:
        pass


class CaptureApp:
    def __init__(self, root):
        self.root = root
        self.ui_queue = queue.Queue()
        self.busy = False
        self.hotkey_ok = None
        self.thumb_refs = []

        try:
            cfg = load_config()
            self.config_error = None
        except ConfigError as exc:
            # Start with defaults but refuse to save over the user's file
            # until they've had a chance to see what went wrong.
            cfg = {}
            self.config_error = str(exc)
        self.custom_presets = dict(cfg.get("custom_presets", {}))
        saved_screens = cfg.get("selected_screens")
        self._initial_selected = set(saved_screens) if saved_screens else None

        root.title("Screens -> ChatGPT")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.configure(bg=BG)
        try:
            root.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if cfg.get("window_pos"):
            try:
                root.geometry(cfg["window_pos"])
            except Exception:
                pass
        root.option_add("*TCombobox*Listbox*Background", PANEL_HI)
        root.option_add("*TCombobox*Listbox*Foreground", TEXT)
        root.option_add("*TCombobox*Listbox*selectBackground", ACCENT)

        outer = tk.Frame(root, bg=BG, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        # -- Header ----------------------------------------------------
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(
            head, text="Screens → ChatGPT", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left")
        self.refresh_btn = tk.Label(
            head, text="↻", bg=BG, fg=MUTED, cursor="hand2",
            font=("Segoe UI", 13),
        )
        self.refresh_btn.pack(side="right")
        self.refresh_btn.bind("<Button-1>", lambda e: self.rebuild_monitor_row())

        self.subtitle_var = tk.StringVar(value="detecting screens…")
        tk.Label(
            outer, textvariable=self.subtitle_var, bg=BG, fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        # -- Monitor previews ------------------------------------------
        self.mon_row = tk.Frame(outer, bg=BG)
        self.mon_row.pack(fill="x", pady=(0, 12))
        self.mon_selected = []
        self.mon_widgets = []
        self.rebuild_monitor_row()

        # -- Prompt ----------------------------------------------------
        preset_row = tk.Frame(outer, bg=BG)
        preset_row.pack(fill="x", pady=(0, 6))
        tk.Label(
            preset_row, text="Quick prompt", bg=BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(side="left")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=PANEL, background=PANEL_HI, foreground=TEXT,
            arrowcolor=MUTED, bordercolor=BORDER_OFF, lightcolor=PANEL,
            darkcolor=PANEL, selectbackground=PANEL, selectforeground=TEXT,
        )
        self.del_btn = tk.Label(
            preset_row, text="✕", bg=BG, fg=MUTED, cursor="hand2",
            font=("Segoe UI", 11),
        )
        self.del_btn.pack(side="right", padx=(6, 0))
        self.del_btn.bind("<Button-1>", lambda e: self.delete_preset())
        self.del_btn.bind("<Enter>", lambda e: self.del_btn.config(fg=DANGER))
        self.del_btn.bind("<Leave>", lambda e: self.del_btn.config(fg=MUTED))

        self.add_btn = tk.Label(
            preset_row, text="＋", bg=BG, fg=MUTED, cursor="hand2",
            font=("Segoe UI", 12),
        )
        self.add_btn.pack(side="right", padx=(8, 0))
        self.add_btn.bind("<Button-1>", lambda e: self.save_preset_dialog())
        self.add_btn.bind("<Enter>", lambda e: self.add_btn.config(fg=ACCENT))
        self.add_btn.bind("<Leave>", lambda e: self.add_btn.config(fg=MUTED))

        self.preset_box = ttk.Combobox(
            preset_row, state="readonly",
            style="Dark.TCombobox", width=22, font=("Segoe UI", 9),
        )
        self.preset_box.pack(side="right")
        self.preset_box.bind("<<ComboboxSelected>>", self.on_preset)
        self.refresh_preset_values()

        self.prompt_box = tk.Text(
            outer, width=52, height=4, wrap="word",
            bg=PANEL, fg=TEXT, insertbackground=ACCENT,
            relief="flat", highlightthickness=1,
            highlightbackground=BORDER_OFF, highlightcolor=ACCENT,
            font=("Segoe UI", 10), padx=8, pady=6,
        )
        self.prompt_box.insert("1.0", cfg.get("last_prompt") or DEFAULT_PROMPT)
        self.prompt_box.pack(fill="x", pady=(0, 8))

        # -- Options + action ------------------------------------------
        opt_kw = dict(
            bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG,
            activeforeground=TEXT, relief="flat", highlightthickness=0,
            font=("Segoe UI", 9), cursor="hand2", anchor="w",
        )
        self.auto_send = tk.BooleanVar(value=bool(cfg.get("auto_send", False)))
        tk.Checkbutton(
            outer, text="Send automatically (press Enter for me)",
            variable=self.auto_send, **opt_kw,
        ).pack(anchor="w")

        self.new_chat = tk.BooleanVar(value=bool(cfg.get("new_chat", False)))
        tk.Checkbutton(
            outer, text="Start a new chat each time (off = keep same chat)",
            variable=self.new_chat, **opt_kw,
        ).pack(anchor="w", pady=(0, 10))

        self.capture_btn = tk.Button(
            outer, text=f"⚡ Capture   ({HOTKEY_LABEL})",
            command=self.on_capture, bg=ACCENT, fg="white",
            activebackground=ACCENT_DIM, activeforeground="white",
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI Semibold", 11), pady=9,
        )
        self.capture_btn.pack(fill="x")
        self.capture_btn.bind(
            "<Enter>", lambda e: self.capture_btn.config(
                bg=ACCENT_DIM if not self.busy else ACCENT)
        )
        self.capture_btn.bind(
            "<Leave>", lambda e: self.capture_btn.config(bg=ACCENT)
        )

        self.status_var = tk.StringVar(value="Ready.")
        self.status_lbl = tk.Label(
            outer, textvariable=self.status_var, bg=BG, fg=MUTED,
            wraplength=420, justify="left", font=("Segoe UI", 9),
        )
        self.status_lbl.pack(anchor="w", pady=(8, 0))

        if self.config_error:
            self.status_var.set(self.config_error)
            self.status_lbl.config(fg=DANGER)

        root.update_idletasks()
        # Top-level Win32 handle of this GUI, so the worker thread can hide
        # it while capturing (keeps it out of the screenshots) and restore
        # it afterwards without stealing focus back from ChatGPT.
        self.gui_hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        enable_dark_titlebar(self.gui_hwnd)

        threading.Thread(target=self.hotkey_listener, daemon=True).start()
        self.root.after(120, self.poll_events)
        self.root.after(5000, self.auto_refresh_thumbs)

    # -- Monitor previews ---------------------------------------------
    def rebuild_monitor_row(self):
        for w in self.mon_row.winfo_children():
            w.destroy()
        monitors = list_monitors()
        pairs = grab_thumbnails()

        old_vals = [v.get() for v in self.mon_selected]
        if not old_vals and self._initial_selected is not None:
            vals = [i in self._initial_selected for i in range(len(monitors))]
            self._initial_selected = None
        else:
            vals = [
                old_vals[i] if i < len(old_vals) else True
                for i in range(len(monitors))
            ]
        self.mon_selected = [tk.BooleanVar(value=v) for v in vals]
        self.mon_widgets = []
        self.thumb_refs = []

        for i, (mon, (img, dimmed)) in enumerate(zip(monitors, pairs)):
            normal = ImageTk.PhotoImage(img)
            dark = ImageTk.PhotoImage(dimmed)
            self.thumb_refs.append((normal, dark))

            cell = tk.Frame(self.mon_row, bg=BG)
            cell.pack(side="left", padx=(0 if i == 0 else 8, 0))

            thumb = tk.Label(
                cell, image=normal, bg=BG, cursor="hand2",
                highlightthickness=2, highlightbackground=ACCENT, bd=0,
            )
            thumb.pack()
            caption = tk.Label(
                cell, text=f"Screen {i + 1} · {mon['width']}×{mon['height']}",
                bg=BG, fg=TEXT, font=("Segoe UI", 8), cursor="hand2",
            )
            caption.pack(pady=(3, 0))

            for w in (thumb, caption):
                w.bind("<Button-1>", lambda e, idx=i: self.toggle_monitor(idx))
            self.mon_widgets.append((thumb, caption))

        for i in range(len(self.mon_widgets)):
            self.apply_monitor_style(i)
        self.update_subtitle()

    def toggle_monitor(self, idx):
        var = self.mon_selected[idx]
        var.set(not var.get())
        self.apply_monitor_style(idx)
        self.update_subtitle()

    def apply_monitor_style(self, idx):
        on = self.mon_selected[idx].get()
        thumb, caption = self.mon_widgets[idx]
        normal, dark = self.thumb_refs[idx]
        thumb.config(
            image=normal if on else dark,
            highlightbackground=ACCENT if on else BORDER_OFF,
        )
        caption.config(fg=TEXT if on else MUTED)

    def update_subtitle(self):
        total = len(self.mon_selected)
        picked = sum(v.get() for v in self.mon_selected)
        hk = (
            f"hotkey {HOTKEY_LABEL}" if self.hotkey_ok
            else "hotkey unavailable" if self.hotkey_ok is False
            else ""
        )
        text = f"{total} screen{'s' if total != 1 else ''} detected · {picked} selected"
        if hk:
            text += f" · {hk}"
        self.subtitle_var.set(text)

    def auto_refresh_thumbs(self):
        if not self.busy:
            try:
                pairs = grab_thumbnails()
                if len(pairs) != len(self.mon_widgets):
                    self.rebuild_monitor_row()
                else:
                    self.thumb_refs = []
                    for i, (img, dimmed) in enumerate(pairs):
                        self.thumb_refs.append(
                            (ImageTk.PhotoImage(img), ImageTk.PhotoImage(dimmed))
                        )
                        self.apply_monitor_style(i)
            except Exception:
                pass
        self.root.after(5000, self.auto_refresh_thumbs)

    # -- Prompt presets -------------------------------------------------
    def refresh_preset_values(self):
        self.preset_box["values"] = (
            list(PRESETS) + ["★ " + k for k in self.custom_presets]
        )

    def on_preset(self, _event):
        name = self.preset_box.get()
        if name.startswith("★ "):
            text = self.custom_presets.get(name[2:])
        else:
            text = PRESETS.get(name)
        if text:
            self.prompt_box.delete("1.0", "end")
            self.prompt_box.insert("1.0", text)

    def save_preset_dialog(self):
        text = self.prompt_box.get("1.0", "end").strip()
        if not text:
            self.set_status("Nothing to save - the prompt box is empty.", DANGER)
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Save prompt")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self.root)

        tk.Label(
            dlg, text="Name this prompt", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=14, pady=(12, 6))
        entry = tk.Entry(
            dlg, bg=PANEL, fg=TEXT, insertbackground=ACCENT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER_OFF,
            highlightcolor=ACCENT, font=("Segoe UI", 10), width=34,
        )
        entry.pack(padx=14, ipady=5, fill="x")
        entry.insert(0, f"My prompt {len(self.custom_presets) + 1}")
        entry.select_range(0, "end")

        result = {"name": None}

        def ok(_event=None):
            n = entry.get().strip()
            if n:
                result["name"] = n
                dlg.destroy()

        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=14, pady=12)
        tk.Button(
            row, text="Save", command=ok, bg=ACCENT, fg="white",
            activebackground=ACCENT_DIM, activeforeground="white",
            relief="flat", bd=0, font=("Segoe UI Semibold", 9),
            padx=18, pady=4, cursor="hand2",
        ).pack(side="right")
        tk.Button(
            row, text="Cancel", command=dlg.destroy, bg=PANEL_HI, fg=MUTED,
            activebackground=PANEL, activeforeground=TEXT,
            relief="flat", bd=0, font=("Segoe UI", 9),
            padx=12, pady=4, cursor="hand2",
        ).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", ok)

        dlg.update_idletasks()
        enable_dark_titlebar(ctypes.windll.user32.GetAncestor(dlg.winfo_id(), 2))
        dlg.geometry(
            f"+{self.root.winfo_rootx() + 30}+{self.root.winfo_rooty() + 140}"
        )
        entry.focus_set()
        dlg.grab_set()
        self.root.wait_window(dlg)

        name = result["name"]
        if not name:
            return
        self.custom_presets[name] = text
        self.save_settings()
        self.refresh_preset_values()
        self.preset_box.set("★ " + name)
        self.set_status(f'Saved prompt "{name}". ✓', ACCENT)

    def delete_preset(self):
        val = self.preset_box.get()
        if val.startswith("★ ") and val[2:] in self.custom_presets:
            name = val[2:]
            del self.custom_presets[name]
            self.save_settings()
            self.refresh_preset_values()
            self.preset_box.set("")
            self.set_status(f'Deleted "{name}".')
        else:
            self.set_status("Pick one of your ★ saved prompts to delete.", DANGER)

    # -- Settings persistence ---------------------------------------------
    def save_settings(self):
        if self.config_error:
            return  # don't clobber a config we couldn't read
        geo = self.root.geometry()
        pos = re.search(r"([+-]\d+[+-]\d+)$", geo)
        save_config({
            "custom_presets": self.custom_presets,
            "auto_send": self.auto_send.get(),
            "new_chat": self.new_chat.get(),
            "selected_screens": [
                i for i, v in enumerate(self.mon_selected) if v.get()
            ],
            "last_prompt": self.prompt_box.get("1.0", "end").strip(),
            "window_pos": pos.group(1) if pos else None,
        })

    def on_close(self):
        try:
            self.save_settings()
        finally:
            self.root.destroy()

    # -- Hotkey ----------------------------------------------------------
    def hotkey_listener(self):
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, HOTKEY_MODS, HOTKEY_VK):
            self.ui_queue.put(("hotkey", False))
            return
        self.ui_queue.put(("hotkey", True))
        msg = wintypes.MSG()
        WM_HOTKEY = 0x0312
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                self.ui_queue.put(("capture", None))

    # -- Event pump (thread-safe UI updates) -----------------------------
    def poll_events(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    text, color = payload
                    self.status_var.set(text)
                    self.status_lbl.config(fg=color)
                elif kind == "busy":
                    self.busy = payload
                    self.capture_btn.config(
                        state="disabled" if payload else "normal",
                        bg=PANEL_HI if payload else ACCENT,
                    )
                elif kind == "capture":
                    self.on_capture()
                elif kind == "save":
                    self.save_settings()
                elif kind == "hotkey":
                    self.hotkey_ok = payload
                    self.update_subtitle()
        except queue.Empty:
            pass
        self.root.after(120, self.poll_events)

    def set_status(self, text, color=MUTED):
        self.ui_queue.put(("status", (text, color)))

    # -- Capture flow ----------------------------------------------------
    def on_capture(self):
        if self.busy:
            return
        selected = [i for i, v in enumerate(self.mon_selected) if v.get()]
        if not selected:
            self.set_status("Select at least one screen first.", DANGER)
            return
        prompt = self.prompt_box.get("1.0", "end").strip()
        auto = self.auto_send.get()
        new_chat = self.new_chat.get()
        self.ui_queue.put(("busy", True))
        threading.Thread(
            target=self.run_capture, args=(selected, prompt, auto, new_chat),
            daemon=True,
        ).start()

    def run_capture(self, selected, prompt, auto, new_chat):
        try:
            hwnd = find_chatgpt_window()
            if hwnd is None:
                self.set_status("ChatGPT window not found. Open the app and retry.", DANGER)
                return

            # Hide this GUI before capturing so it doesn't show up in the
            # screenshots that get sent.
            win32gui.ShowWindow(self.gui_hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.4)

            self.set_status("Capturing screens…")
            paths = capture_screens(selected)

            self.set_status("Switching to ChatGPT…")
            if not focus_and_click_composer(hwnd, new_chat):
                self.set_status(
                    "Couldn't bring ChatGPT to the front. Click its window "
                    "once yourself, then try again.", DANGER,
                )
                return

            for n, path in enumerate(paths, start=1):
                self.set_status(f"Pasting screen {n}/{len(paths)}…")
                set_clipboard_image(path)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(PASTE_DELAY)

            if prompt:
                # Paste rather than type: pyautogui.write() sends a real
                # Enter for each newline, which ChatGPT reads as "send" -
                # that silently split or mangled multi-line prompts.
                set_clipboard_text(prompt)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.5)

            if auto:
                pyautogui.press("enter")
                self.set_status(f"Sent {len(paths)} screen(s). ✓", ACCENT)
            else:
                self.set_status(
                    f"Staged {len(paths)} screen(s) - review in ChatGPT "
                    "and press Enter. ✓", ACCENT,
                )
            winsound.MessageBeep(winsound.MB_ICONASTERISK)

            for p in paths:
                os.remove(p)
        except Exception as exc:
            self.set_status(f"Error: {exc}", DANGER)
        finally:
            # Bring the GUI back without activating it, so keyboard focus
            # stays in ChatGPT's composer.
            win32gui.ShowWindow(self.gui_hwnd, win32con.SW_SHOWNOACTIVATE)
            self.ui_queue.put(("busy", False))
            self.ui_queue.put(("save", None))


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Legion.ScreensToChatGPT"
        )
    except Exception:
        pass
    root = tk.Tk()
    CaptureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
