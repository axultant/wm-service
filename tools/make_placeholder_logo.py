"""Generate a placeholder RGBA logo at assets/logo.png (only if missing)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

out = Path(__file__).resolve().parent.parent / "assets" / "logo.png"
if out.exists():
    print("exists:", out)
else:
    W, H = 1400, 420
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=60, fill=(20, 40, 80, 170), outline=(255, 255, 255, 230), width=10)
    d.ellipse((50, 60, 350, 360), fill=(255, 255, 255, 235))
    d.ellipse((120, 130, 280, 290), fill=(20, 40, 80, 255))
    font = ImageFont.load_default(size=150)
    d.text((400, H // 2), "LOGO", font=font, fill=(255, 255, 255, 240), anchor="lm")
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    print("created:", out)
