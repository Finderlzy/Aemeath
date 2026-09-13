# -*- coding: utf-8 -*-
"""Generate two vision test images: random text + two shapes of different colors."""
import random
import string
import sys
import os

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join("data", "acceptance", "vision-test")

CASES = [
    ("red", "blue", ("circle", "square")),
    ("green", "orange", ("triangle", "circle")),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    random.seed(20260912)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 40)
    for i, (color_a, color_b, shapes) in enumerate(CASES):
        img = Image.new("RGB", (800, 500), "white")
        draw = ImageDraw.Draw(img)
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        number = "WX-%d" % random.randint(100, 999)
        draw.text((60, 50), "Code: " + code, fill="black", font=font)
        draw.text((60, 120), "No. " + number, fill="black", font=font)
        for j, (color, shape) in enumerate(zip((color_a, color_b), shapes)):
            x, y = 100 + j * 350, 250
            if shape == "circle":
                draw.ellipse([x, y, x + 160, y + 160], fill=color)
            elif shape == "square":
                draw.rectangle([x, y, x + 160, y + 160], fill=color)
            else:
                draw.polygon([(x + 80, y), (x + 160, y + 160), (x, y + 160)], fill=color)
        path = os.path.join(OUT_DIR, "test%d.png" % (i + 1))
        img.save(path)
        print(path, code, number)


if __name__ == "__main__":
    sys.exit(main())
