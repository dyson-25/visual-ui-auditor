import math
from pathlib import Path

from PIL import Image, ImageDraw


# Global report numbers keep exported badges aligned with the interactive inspector.
def draw_heatmap(source: Path, destination: Path, bugs: list[dict]) -> None:
    with Image.open(source) as raw:
        image = raw.convert("RGBA")
    overlay = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(overlay)
    for number, bug in enumerate(bugs, 1):
        rect = bug.get("rect")
        if not rect or not all(math.isfinite(rect[k]) for k in ("x", "y", "width", "height")):
            continue
        x, y = max(0, rect["x"]), max(0, rect["y"])
        right = min(image.width - 1, rect["x"] + rect["width"])
        bottom = min(image.height - 1, rect["y"] + rect["height"])
        if right <= x or bottom <= y:
            continue
        color = (255, 75, 97) if bug["severity"] == "high" else (255, 184, 77)
        draw.rectangle((x, y, right, bottom), fill=(*color, 25))
        for width, alpha in ((7, 35), (4, 95), (2, 240)):
            draw.rectangle((x, y, right, bottom), outline=(*color, alpha), width=width)
        label = str(bug.get("number") or number)
        label_width = max(20, len(label) * 7 + 10)
        draw.rounded_rectangle((x, y, x + label_width, y + 19), radius=4, fill=(*color, 255))
        draw.text((x + 5, y + 3), label, fill=(20, 22, 30, 255))
    Image.alpha_composite(image, overlay).convert("RGB").save(destination)
