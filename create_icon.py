#!/usr/bin/env python3
"""Generate app.ico for ScreenToChatGPT: green lightning bolt on a dark
rounded tile, with a soft glow. Re-run any time to regenerate."""

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
BG = (14, 18, 24, 255)
EDGE = (16, 163, 127, 255)
BOLT = (16, 163, 127, 255)
BOLT_HI = (82, 227, 185, 255)

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([10, 10, SIZE - 10, SIZE - 10], radius=58, fill=BG,
                    outline=EDGE, width=7)

bolt = [(150, 34), (84, 142), (122, 142), (104, 224), (176, 116), (136, 116)]

glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
ImageDraw.Draw(glow).polygon(bolt, fill=BOLT)
glow = glow.filter(ImageFilter.GaussianBlur(12))
img.alpha_composite(glow)
img.alpha_composite(glow)

d = ImageDraw.Draw(img)
d.polygon(bolt, fill=BOLT_HI)

img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                           (64, 64), (128, 128), (256, 256)])
img.resize((128, 128), Image.LANCZOS).save("app_preview.png")
print("wrote app.ico + app_preview.png")
