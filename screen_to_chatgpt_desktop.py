#!/usr/bin/env python3
"""Capture every monitor and paste them into the ChatGPT desktop app.

Usage:
    python screen_to_chatgpt_desktop.py ["your question"] [--send] [--keep] [--save-dir DIR]

Requires the ChatGPT desktop app (Windows) to already be open and logged in.
By default this pastes your screenshots + question into the chat box and
STOPS without sending, so you can review the attachments first. Pass --send
to have it press Enter automatically.
"""

import argparse
import ctypes
from ctypes import wintypes
import datetime
import io
import os
import sys
import time

import mss
import mss.tools
import pyautogui
import pygetwindow as gw
import win32clipboard
import win32con
import win32gui
from PIL import Image

DEFAULT_PROMPT = (
    "Here are my 3 monitors of ATAS trading charts. What do you see - "
    "any notable setups, levels, or order flow worth flagging?"
)
WINDOW_TITLE_HINT = "ChatGPT"
# The real desktop app's process - matching on window title alone is not
# enough, because OTHER windows can have "ChatGPT" in their title, and
# pasting would silently go to the wrong one.
CHATGPT_EXE_NAMES = {"chatgpt.exe", "chatgpt classic.exe"}
PASTE_DELAY = 1.2  # seconds to let each image attach/render before the next paste


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


def capture_screens(save_dir):
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = []
    with mss.mss() as sct:
        for i, monitor in enumerate(sct.monitors[1:], start=1):
            shot = sct.grab(monitor)
            path = os.path.join(save_dir, f"screen_{timestamp}_{i}.png")
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


def focus_chatgpt_window(new_chat=False):
    hwnd = find_chatgpt_window()
    if hwnd is None:
        sys.exit(
            "Couldn't find a ChatGPT desktop window. Open the ChatGPT app "
            "(a real chat window, not just the tray quick-launcher) and try again."
        )

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

    if win32gui.GetForegroundWindow() != hwnd:
        sys.exit(
            "Couldn't bring ChatGPT to the front. Click on the ChatGPT "
            "window once yourself, then run this again."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument("--save-dir", default="screenshots")
    parser.add_argument(
        "--keep", action="store_true",
        help="Keep the screenshot files after pasting (default: delete)",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Press Enter to send automatically (default: stages it for you to review and send)",
    )
    parser.add_argument(
        "--new-chat", action="store_true",
        help="Start a new conversation (default: use whatever chat is open)",
    )
    args = parser.parse_args()

    print("Capturing screens...")
    paths = capture_screens(args.save_dir)
    print(f"Captured {len(paths)} screen(s).")

    print("Switching to ChatGPT desktop app...")
    focus_chatgpt_window(args.new_chat)

    for path in paths:
        set_clipboard_image(path)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(PASTE_DELAY)

    if args.prompt:
        # Paste rather than type: pyautogui.write() sends a real Enter for
        # each newline, which ChatGPT reads as "send" - that silently split
        # or mangled multi-line prompts.
        set_clipboard_text(args.prompt)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.5)

    if args.send:
        pyautogui.press("enter")
        print("Sent.")
    else:
        print("Staged in ChatGPT — review the attached images and press Enter to send.")

    if not args.keep:
        for p in paths:
            os.remove(p)


if __name__ == "__main__":
    main()
