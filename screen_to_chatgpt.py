#!/usr/bin/env python3
"""Capture every connected monitor and ask ChatGPT (GPT-4o) about them.

Usage:
    python screen_to_chatgpt.py ["your question"] [--keep] [--save-dir DIR]

Requires the OPENAI_API_KEY environment variable to be set.
"""

import argparse
import base64
import datetime
import os
import sys

import mss
import mss.tools
from openai import OpenAI

DEFAULT_PROMPT = (
    "Describe what's on my screen(s). Point out anything that looks "
    "important, broken, or actionable."
)
MODEL = "gpt-4o"


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


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def ask_chatgpt(image_paths, prompt, api_key):
    client = OpenAI(api_key=api_key)
    content = [{"type": "text", "text": prompt}]
    for path in image_paths:
        b64 = encode_image(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": content}],
    )
    return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "prompt", nargs="?", default=DEFAULT_PROMPT,
        help="Question to ask about your screens",
    )
    parser.add_argument(
        "--save-dir", default="screenshots",
        help="Where to save captured screenshots (default: ./screenshots)",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="Keep the screenshot files after sending (default: delete them)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(
            "OPENAI_API_KEY is not set. In PowerShell:\n"
            '  $env:OPENAI_API_KEY = "sk-..."\n'
            "(or setx OPENAI_API_KEY \"sk-...\" to persist it)"
        )

    print("Capturing screens...")
    paths = capture_screens(args.save_dir)
    print(f"Captured {len(paths)} screen(s): {', '.join(paths)}")

    print("Asking ChatGPT...")
    try:
        answer = ask_chatgpt(paths, args.prompt, api_key)
    finally:
        if not args.keep:
            for p in paths:
                os.remove(p)

    print("\n--- ChatGPT response ---\n")
    print(answer)


if __name__ == "__main__":
    main()
