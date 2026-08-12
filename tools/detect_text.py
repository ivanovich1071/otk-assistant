"""Поиск текстовых блоков на растровом чертеже.

Выдаёт точные bbox и ориентацию (0° / 90°) каждой надписи. Содержимое блоков
не распознаётся — это задача агента, который читает увеличенные кропы.

Использование:
    python tools/detect_text.py <чертёж> [-o work/blocks.json] [--debug work/blocks.png]
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import cv2
import numpy as np


# Штриховка и осевые линии на чертеже тоньше цифр лишь чуть-чуть, поэтому
# отбор идёт не по толщине штриха, а по габаритам и заполненности bbox.
MIN_GLYPH_H = 6
MAX_GLYPH_H = 70
MIN_GLYPH_W = 2
MAX_GLYPH_W = 70
MIN_GLYPH_AREA = 12
MAX_GLYPH_FILL = 0.92
MIN_GLYPH_FILL = 0.11        # ниже — штрих штриховки или кусок дуги, а не символ
ARROW_FILL = 0.42            # стрелка залита плотнее любой цифры
ARROW_ASPECT = 0.5           # и заметно вытянута вдоль размерной линии


@dataclass
class Block:
    id: int
    x: int
    y: int
    w: int
    h: int
    angle: int          # 0 — горизонтальная надпись, 90 — повёрнутая
    glyphs: int
    ink: int            # число «чернильных» пикселей, для отсева мусора


def load_gray(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit(f"Не удалось прочитать изображение: {path}")
    return img


def binarize(gray: np.ndarray) -> np.ndarray:
    """Чёрное на белом -> маска, где чернила = 255."""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return bw


def estimate_text_height(bw: np.ndarray) -> float:
    """Медианная высота мелких компонент — масштаб шрифта чертежа."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    hs = [
        stats[i, cv2.CC_STAT_HEIGHT]
        for i in range(1, n)
        if MIN_GLYPH_H <= stats[i, cv2.CC_STAT_HEIGHT] <= MAX_GLYPH_H
        and stats[i, cv2.CC_STAT_WIDTH] <= MAX_GLYPH_W
    ]
    return float(np.median(hs)) if hs else 20.0


def remove_long_lines(bw: np.ndarray, min_len: int) -> tuple[np.ndarray, np.ndarray]:
    """Убирает выносные, размерные и контурные линии, чтобы остались только глифы.

    Возвращает (маска глифов, маска линий) — вторая нужна, чтобы отличить
    стрелку размера от цифры: стрелка всегда насажена на линию."""
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len))
    lines = cv2.bitwise_or(
        cv2.morphologyEx(bw, cv2.MORPH_OPEN, h_kernel),
        cv2.morphologyEx(bw, cv2.MORPH_OPEN, v_kernel),
    )
    # Линия толще одного пикселя: расширяем маску, иначе от неё остаётся бахрома,
    # которая склеивает соседние глифы в один блок.
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    return cv2.subtract(bw, lines), lines


def find_glyphs(
    mask: np.ndarray, lines: np.ndarray, text_h: float
) -> list[tuple[int, int, int, int, int, bool]]:
    """Компоненты-кандидаты в символы: (x, y, w, h, area, похоже_на_стрелку)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    lo_h = max(MIN_GLYPH_H, int(text_h * 0.25))
    hi_h = min(MAX_GLYPH_H, int(text_h * 2.6))
    hi_w = max(MAX_GLYPH_W, int(text_h * 2.6))
    out = []
    for i in range(1, n):
        x, y, w, h, area = (
            stats[i, cv2.CC_STAT_LEFT],
            stats[i, cv2.CC_STAT_TOP],
            stats[i, cv2.CC_STAT_WIDTH],
            stats[i, cv2.CC_STAT_HEIGHT],
            stats[i, cv2.CC_STAT_AREA],
        )
        if area < MIN_GLYPH_AREA or not (lo_h <= h <= hi_h) or not (MIN_GLYPH_W <= w <= hi_w):
            continue
        fill = area / float(w * h)
        if fill > MAX_GLYPH_FILL or fill < MIN_GLYPH_FILL:
            continue  # сплошной блик либо тонкий штрих штриховки/дуги
        if w > text_h * 1.6 and h > text_h * 1.6 and fill < 0.22:
            continue  # крупный разреженный обломок дуги или штриховки
        aspect = min(w, h) / float(max(w, h))
        # Линия удалена с запасом в 3 px, поэтому стрелка распадается на «щепки»
        # рядом с линией — ищем линию в расширенной рамке, а не строго в bbox.
        pad = max(4, int(text_h * 0.25))
        y0, y1 = max(0, y - pad), min(lines.shape[0], y + h + pad)
        x0, x1 = max(0, x - pad), min(lines.shape[1], x + w + pad)
        on_line = bool(lines[y0:y1, x0:x1].any())
        arrowish = fill >= ARROW_FILL and aspect <= ARROW_ASPECT and on_line
        out.append((x, y, w, h, area, arrowish))
    return out


def _group(
    glyphs: list[tuple], text_h: float, vertical: bool
) -> list[list[int]]:
    """Сцепляет глифы в строки вдоль оси: по X для горизонтали, по Y для вертикали."""
    gap = text_h * 1.15          # допустимый разрыв между символами
    drift = text_h * 0.55        # допустимый сдвиг поперёк строки

    order = sorted(
        range(len(glyphs)),
        key=lambda i: (glyphs[i][1], glyphs[i][0]) if vertical else (glyphs[i][0], glyphs[i][1]),
    )
    parent = list(range(len(glyphs)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Скользящее окно по оси строки: сравниваем только близких соседей.
    for pos, i in enumerate(order):
        xi, yi, wi, hi = glyphs[i][:4]
        end_i = (yi + hi) if vertical else (xi + wi)
        mid_i = (xi + wi / 2) if vertical else (yi + hi / 2)
        for j in order[pos + 1 : pos + 40]:
            xj, yj, wj, hj = glyphs[j][:4]
            start_j = yj if vertical else xj
            mid_j = (xj + wj / 2) if vertical else (yj + hj / 2)
            if start_j - end_i > gap:
                break
            if start_j < (yi if vertical else xi) - gap:
                continue
            if abs(mid_i - mid_j) <= drift:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(glyphs)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _bbox(glyphs: list[tuple], idx: list[int]) -> tuple[int, int, int, int]:
    xs = [glyphs[i][0] for i in idx]
    ys = [glyphs[i][1] for i in idx]
    xe = [glyphs[i][0] + glyphs[i][2] for i in idx]
    ye = [glyphs[i][1] + glyphs[i][3] for i in idx]
    return min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys)


def build_blocks(glyphs: list[tuple], text_h: float) -> list[Block]:
    """Каждый глиф попадает в ту цепочку — горизонтальную или вертикальную, —
    где у него больше соседей. Одиночки остаются отдельными блоками (Ø, R, буквы видов)."""
    h_groups = _group(glyphs, text_h, vertical=False)
    v_groups = _group(glyphs, text_h, vertical=True)

    h_of = {i: g for g in h_groups for i in g}
    v_of = {i: g for g in v_groups for i in g}

    chosen: dict[int, tuple[str, tuple[int, ...]]] = {}
    for i in range(len(glyphs)):
        hg, vg = h_of.get(i, [i]), v_of.get(i, [i])
        if len(vg) > len(hg):
            chosen[i] = ("v", tuple(sorted(vg)))
        else:
            chosen[i] = ("h", tuple(sorted(hg)))

    # Цепочка принимается целиком только теми глифами, которые её выбрали:
    # иначе вертикальный размер, задевший горизонтальную надпись, склеит обе.
    buckets: dict[tuple[str, tuple[int, ...]], list[int]] = {}
    for i, key in chosen.items():
        buckets.setdefault(key, []).append(i)

    blocks: list[Block] = []
    for (axis, _), idx in buckets.items():
        if all(glyphs[i][5] for i in idx):
            continue  # блок целиком из стрелок размерных линий
        x, y, w, h = _bbox(glyphs, idx)
        if max(w, h) > text_h * 30:
            continue  # склейка через полчертежа — не надпись
        ink = sum(glyphs[i][4] for i in idx)
        blocks.append(
            Block(id=0, x=int(x), y=int(y), w=int(w), h=int(h),
                  angle=90 if axis == "v" else 0, glyphs=len(idx), ink=int(ink))
        )

    blocks.sort(key=lambda b: (b.y, b.x))
    for n, b in enumerate(blocks, 1):
        b.id = n
    return blocks


def detect(path: Path) -> dict:
    gray = load_gray(path)
    bw = binarize(gray)
    text_h = estimate_text_height(bw)
    min_len = max(24, int(text_h * 2.2))
    mask, lines = remove_long_lines(bw, min_len)
    glyphs = find_glyphs(mask, lines, text_h)
    blocks = build_blocks(glyphs, text_h)
    return {
        "image": str(path),
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
        "text_height": round(text_h, 1),
        "glyphs": len(glyphs),
        "blocks": [asdict(b) for b in blocks],
    }


def draw_debug(path: Path, result: dict, out: Path) -> None:
    gray = load_gray(path)
    canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    for b in result["blocks"]:
        color = (0, 0, 255) if b["angle"] == 0 else (255, 0, 0)
        cv2.rectangle(canvas, (b["x"], b["y"]), (b["x"] + b["w"], b["y"] + b["h"]), color, 2)
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out.write_bytes(buf.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description="Поиск текстовых блоков на чертеже")
    ap.add_argument("image", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("work/blocks.json"))
    ap.add_argument("--debug", type=Path, help="PNG с обведёнными блоками")
    ap.add_argument("--min-glyphs", type=int, default=1, help="отбросить блоки короче N символов")
    args = ap.parse_args()

    result = detect(args.image)
    if args.min_glyphs > 1:
        result["blocks"] = [b for b in result["blocks"] if b["glyphs"] >= args.min_glyphs]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.debug:
        args.debug.parent.mkdir(parents=True, exist_ok=True)
        draw_debug(args.image, result, args.debug)

    horiz = sum(1 for b in result["blocks"] if b["angle"] == 0)
    print(f"глифов: {result['glyphs']}, блоков: {len(result['blocks'])} "
          f"(горизонт. {horiz}, поверн. {len(result['blocks']) - horiz}), "
          f"высота шрифта ~{result['text_height']} px")
    print(f"-> {args.out}" + (f", {args.debug}" if args.debug else ""))


if __name__ == "__main__":
    main()
