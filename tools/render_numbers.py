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
MIN_FONT = 9                 # мельче номер не читается даже при увеличении
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


def _ink(occupied: np.ndarray, x: int, y: int, w: int, h: int, pad: int) -> float:
    """Сколько чернил под меткой вместе с полем вокруг неё. За краем листа — всё."""
    H, W = occupied.shape
    if x - pad < 0 or y - pad < 0 or x + w + pad > W or y + h + pad > H:
        return 1.0
    window = occupied[y - pad : y + h + pad, x - pad : x + w + pad]
    return float((window > 0).mean())


def _candidates(box: tuple[int, int, int, int], lw: int, lh: int, step: int):
    """Места вокруг надписи, кольцами от неё: сначала вплотную, потом дальше."""
    bx, by, bw, bh = box
    for ring in range(1, 9):
        d = step * ring
        yield from [
            (bx - lw - d, by - lh - d // 2),          # слева сверху — как в ручной разметке
            (bx - lw - d, by),
            (bx, by - lh - d),
            (bx + bw + d, by - lh - d // 2),
            (bx + bw + d, by),
            (bx, by + bh + d),
            (bx - lw - d, by + bh + d),
            (bx + bw + d, by + bh + d),
        ]


def place(occupied: np.ndarray, box: tuple[int, int, int, int],
          lw: int, lh: int, step: int, pad: int = 0) -> tuple[int, int] | None:
    """Свободное место под номер вплотную к надписи, или None, если его нет.

    Поле `pad` вокруг метки обязательно: без него рамка номера садится
    на соседний глиф и в просмотрщике закрывает сам размер.
    """
    for cx, cy in _candidates(box, lw, lh, step):
        if _ink(occupied, int(cx), int(cy), lw, lh, pad) == 0.0:
            return int(cx), int(cy)
    return None


def squeeze(occupied: np.ndarray, box: tuple[int, int, int, int],
            label: str, font_size: int, step: int, pad: int):
    """Место под номер: если вплотную тесно — уменьшаем метку, а не отодвигаем.

    Отодвинуть номер к свободному полю можно, но тогда непонятно, к какому
    размеру он относится: на плотном чертеже между ними окажется полдесятка
    чужих. Мельче — читаемо, далеко — нет.
    """
    best = None
    for scale in (1.0, 0.8, 0.65, 0.5):
        font = load_font(max(MIN_FONT, int(font_size * scale)))
        left, top, right, bottom = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox(
            (0, 0), label, font=font)
        lw, lh = right - left + 4, bottom - top + 4
        spot = place(occupied, box, lw, lh, step, pad)
        if spot:
            return spot, font, (left, top), (lw, lh)
        # Ничего свободного — запоминаем наименее занятое на случай, если так
        # и не найдём: чистого места на плотном узле не бывает вовсе.
        for cx, cy in _candidates(box, lw, lh, step):
            share = _ink(occupied, int(cx), int(cy), lw, lh, 0)
            if best is None or share < best[0]:
                best = (share, (int(cx), int(cy)), font, (left, top), (lw, lh))
    return best[1], best[2], best[3], best[4]


def render(drawing: Path, markup: dict, out: Path, font_scale: float = 1.0) -> int:
    img, bw = ink_mask(drawing)
    occupied = cv2.dilate(bw, np.ones((3, 3), np.uint8))

    text_h = float(markup.get("text_height") or 20.0)
    # Кегль листа крупнее оценки детектора; отступы меряем по нему, а размер
    # самой метки оставляем прежним — она и так не должна спорить с чертежом.
    pitch = float(markup.get("pitch") or text_h)
    size = max(12, int(text_h * 1.05 * font_scale))
    step = max(4, int(pitch * 0.35))
    pad = max(1, int(pitch * 0.15))

    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)

    placed = 0
    for group in markup["groups"]:
        for item in group["items"]:
            label = str(item["no"])
            box = (int(item["x"]), int(item["y"]), int(item["w"]), int(item["h"]))
            if item.get("label_x") is not None and item.get("label_y") is not None:
                # Номер переставлен руками — место выбрано человеком, не сжимаем.
                font = load_font(size)
                l, t, r, b = draw.textbbox((0, 0), label, font=font)
                lw, lh = r - l + 4, b - t + 4
                lx, ly = int(item["label_x"]), int(item["label_y"])
            else:
                (lx, ly), font, (l, t), (lw, lh) = squeeze(
                    occupied, box, label, size, step, pad)
            item["label_x"], item["label_y"] = lx, ly
            item["label_w"], item["label_h"] = lw, lh
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
