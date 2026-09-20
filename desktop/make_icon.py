"""Generate a 1024x1024 app-icon master PNG (football-field motif, on-brand
with the dashboard). See README.md for conversion to .icns."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded background tile + subtle border.
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=200, fill=(14, 20, 34, 255),
                    outline=(36, 48, 67, 255), width=10)
# A faint raised panel for depth.
d.rounded_rectangle([90, 150, S - 90, S - 110], radius=64, fill=(18, 26, 44, 255))

def bar(y, x0, x1, color, tick):
    d.rounded_rectangle([x0, y, x1, y + 96], radius=48, fill=color)
    # white base marker
    d.rounded_rectangle([tick - 7, y - 14, tick + 7, y + 110], radius=7,
                        fill=(230, 237, 246, 255))

# Three valuation ranges (amber / blue / green) — like the football field.
bar(300, 330, 720, (251, 191, 36, 255), 560)   # 52-wk
bar(470, 250, 600, (91, 157, 255, 255), 430)    # DCF
bar(640, 360, 770, (52, 211, 153, 255), 600)    # comps

# Vertical "current price" reference line through the chart.
d.rounded_rectangle([612, 250, 628, 760], radius=8, fill=(91, 157, 255, 230))

output = Path(__file__).resolve().parent / "assets" / "icon_master.png"
output.parent.mkdir(parents=True, exist_ok=True)
img.save(output)
print(f"wrote {output}")
