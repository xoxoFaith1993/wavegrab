"""Draws the Wavegrab icon -> wavegrab/assets/icon.png + icon.ico"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "wavegrab", "assets")
N = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main():
    os.makedirs(OUT, exist_ok=True)
    top, bottom = (255, 77, 141), (124, 92, 255)
    grad = Image.new("RGB", (N, N))
    px = grad.load()
    for y in range(N):
        for x in range(N):
            px[x, y] = lerp(top, bottom, min(1, (x * 0.35 + y) / (N * 1.35)))
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, N - 40, N - 40), radius=230, fill=255)
    icon = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(icon)
    white = (255, 255, 255, 255)
    soft = (255, 255, 255, 150)
    # sound-wave bars either side
    bw, cy = 62, 470
    for off, h in ((300, 300), (410, 170)):
        for x in (N / 2 - off, N / 2 + off):
            d.rounded_rectangle((x - bw / 2, cy - h / 2, x + bw / 2, cy + h / 2), radius=bw / 2, fill=soft)
    # download arrow in the middle
    cx, sw = N / 2, 92
    d.rounded_rectangle((cx - sw / 2, 230, cx + sw / 2, 600), radius=sw / 2, fill=white)
    head = [(cx - 190, 470), (cx, 680), (cx + 190, 470)]
    d.line(head, fill=white, width=sw, joint="curve")
    for p in (head[0], head[2]):
        d.ellipse((p[0] - sw / 2, p[1] - sw / 2, p[0] + sw / 2, p[1] + sw / 2), fill=white)
    d.ellipse((cx - sw / 2, 680 - sw / 2, cx + sw / 2, 680 + sw / 2), fill=white)
    # base line
    d.rounded_rectangle((cx - 250, 770, cx + 250, 770 + sw), radius=sw / 2, fill=white)

    big = icon.resize((256, 256), Image.LANCZOS)
    big.save(os.path.join(OUT, "icon.png"))
    big.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written to", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
