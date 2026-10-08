"""Genera assets/drivemonitor.ico y .png (requiere Pillow: pip install pillow)."""
from PIL import Image, ImageDraw
S = 1024  # se dibuja grande y se reduce para que quede suave
import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# fondo: cuadro redondeado oscuro con borde
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill="#1f2328", outline="#3a4048", width=24)
# anillo del motor/encoder
c, r, w = S // 2, 330, 70
d.ellipse((c - r, c - r, c + r, c + r), outline="#3d8bd9", width=w)
# marcas del encoder sobre el anillo
import math
for i in range(12):
    a = i * math.pi / 6
    x1, y1 = c + (r + 20) * math.cos(a), c + (r + 20) * math.sin(a)
    x2, y2 = c + (r + 75) * math.cos(a), c + (r + 75) * math.sin(a)
    d.line((x1, y1, x2, y2), fill="#3d8bd9", width=28)
# línea de señal (pulso) en verde
pts = [(c - 300, c + 20), (c - 150, c + 20), (c - 90, c - 160), (c - 10, c + 190), (c + 70, c - 90),
       (c + 130, c + 20), (c + 300, c + 20)]
d.line(pts, fill="#3fb950", width=64, joint="curve")
for p in (pts[0], pts[-1]):
    d.ellipse((p[0] - 32, p[1] - 32, p[0] + 32, p[1] + 32), fill="#3fb950")
big = img.resize((256, 256), Image.LANCZOS)
big.save("assets/drivemonitor.png")
big.save("assets/drivemonitor.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
