"""Нанесение красной нумерации на чертёж.

Номер ставится рядом со своей надписью в ближайшее свободное место: сначала
пробуются позиции вплотную к рамке надписи, затем всё дальше. Занятыми считаются
и графика чертежа, и уже поставленные номера.

Использование:
    python tools/render_numbers.py <чертёж> work/markup.json -o "output/... (карта обмера).jpg"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


RED = (220, 0, 0)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def ink_mask(path: Path) -> tuple[np.ndarray, np.ndarray]:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"Не удалось прочитать изображение: {path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return img, bw


def _free(occupied: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
    H, W = occupied.shape
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return False
    return not occupied[y : y + h, x : x + w].any()


def place(occupied: np.ndarray, box: tuple[int, int, int, int],
          lw: int, lh: int, step: int) -> tuple[int, int]:
    """Ищет свободное место под номер, расходясь кольцами от рамки надписи."""
    bx, by, bw, bh = box
    for ring in range(1, 9):
        d = step * ring
        candidates = [
            (bx - lw - d, by - lh - d // 2),          # слева сверху — как в ручной разметке
            (bx - lw - d, by),
            (bx, by - lh - d),
            (bx + bw + d, by - lh - d // 2),
            (bx + bw + d, by),
            (bx, by + bh + d),
            (bx - lw - d, by + bh + d),
            (bx + bw + d, by + bh + d),
        ]
        for cx, cy in candidates:
            if _free(occupied, int(cx), int(cy), lw, lh):
                return int(cx), int(cy)
    return int(bx), int(by - lh - step)


def render(drawing: Path, markup: dict, out: Path, font_scale: float = 1.0) -> int:
    img, bw = ink_mask(drawing)
    occupied = cv2.dilate(bw, np.ones((3, 3), np.uint8))

    text_h = float(markup.get("text_height") or 20.0)
    size = max(12, int(text_h * 1.05 * font_scale))
    font = load_font(size)
    step = max(4, int(text_h * 0.35))

    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)

    placed = 0
    for group in markup["groups"]:
        for item in group["items"]:
            label = str(item["no"])
            l, t, r, b = draw.textbbox((0, 0), label, font=font)
            lw, lh = r - l + 4, b - t + 4
            box = (int(item["x"]), int(item["y"]), int(item["w"]), int(item["h"]))
            if item.get("label_x") is not None and item.get("label_y") is not None:
                lx, ly = int(item["label_x"]), int(item["label_y"])
            else:
                lx, ly = place(occupied, box, lw, lh, step)
                item["label_x"], item["label_y"] = lx, ly
            draw.text((lx + 2 - l, ly + 2 - t), label, font=font, fill=RED)
            occupied[max(0, ly) : ly + lh, max(0, lx) : lx + lw] = 255
            placed += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    result = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    ext = out.suffix.lower() or ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, result, params)
    if not ok:
        raise SystemExit(f"Не удалось записать {out}")
    out.write_bytes(buf.tobytes())
    return placed


def main() -> None:
    ap = argparse.ArgumentParser(description="Красная нумерация размеров на чертеже")
    ap.add_argument("image", type=Path)
    ap.add_argument("markup", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--font-scale", type=float, default=1.0)
    ap.add_argument("--save-markup", action="store_true",
                    help="записать выбранные координаты номеров обратно в markup.json")
    args = ap.parse_args()

    markup = json.loads(args.markup.read_text(encoding="utf-8"))
    n = render(args.image, markup, args.out, args.font_scale)
    if args.save_markup:
        args.markup.write_text(json.dumps(markup, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"нанесено номеров: {n} -> {args.out}")


if __name__ == "__main__":
    main()
