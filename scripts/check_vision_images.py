# -*- coding: utf-8 -*-
"""Live vision check against the production adapter with two distinct images.

Run from the Aemeath root with the acceptance venv:

    vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_vision_images.py

Uses aemeath.adapters.OpenAICompatibleVision (the same class the runtime uses)
and a neutral prompt that does not reveal the expected answer.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aemeath.adapters import OpenAICompatibleVision

IMAGES = [
    "data/acceptance/vision-test/test1.png",
    "data/acceptance/vision-test/test2.png",
]

PROMPT = "描述这张图片的内容，包括图中出现的文字和图形。"


async def main() -> int:
    api_key = os.environ.get("AEMEATH_VISION_API_KEY", "")
    if not api_key:
        print("AEMEATH_VISION_API_KEY is not set")
        return 1
    vision = OpenAICompatibleVision(
        base_url="http://127.0.0.1:8317/v1",
        api_key=api_key,
        model="gemini-3.8-flash-high",
    )
    for path in IMAGES:
        with open(path, "rb") as f:
            image = f.read()
        # Neutral prompt via window_title-free describe; describe() builds its
        # own prompt, so call it directly — it does not reveal the answer.
        description = await vision.describe(image)
        print(f"--- {path} ---")
        print(description)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
