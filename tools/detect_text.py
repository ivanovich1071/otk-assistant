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


def kegel(blocks: list[Block], text_h: float) -> float:
    """Настоящая высота цифры на листе.

    `estimate_text_height` берёт медиану по всем мелким компонентам, а в них
    входят обломки штриховки и засечки: на «Колесе тяговом» она дала 31 px при
    реальных 45. По этой заниженной оценке `_group` разрывал надписи там, где
    разрыв между символами всего чуть шире обычного, — и `501-3653.01`
    приходило в карту двумя позициями, а наклонный `Ø192*` — четырьмя.
    """
    tall = [b.h for b in blocks if b.h >= text_h]
    return float(np.median(tall)) if tall else float(text_h)


# Наклонную надпись — размер на выноске — детектор видит лесенкой из двух-четырёх
# рамок: он знает только 0° и 90°, а цепочка по строке на склоне рвётся сдвигом
# поперёк. Такие рамки заходят друг на друга, чего у соседних надписей не бывает.
MERGE_GAP = 0.25             # зазор между рамками одной надписи, в кеглях
MERGE_SPAN = 8.0             # склейка длиннее — уже не надпись, а полстроки
# Поперёк строки надпись не растёт: даже на склоне лесенка из рамок укладывается
# в три кегля. Без этого предела на «Колесе зубчатом» слипались четыре строки
# таблицы параметров — а их карта разбирает построчно.
MERGE_CROSS = 3.0
CHAIN_SHARE = 0.5            # цепочка шире половины листа — точно не надпись
MERGE_PASSES = 4             # больше сходящихся проходов склейка не требует


def merge_overlapping(blocks: list[Block], pitch: float) -> list[Block]:
    """Сцепляет рамки, наехавшие друг на друга, в одну надпись.

    Проход повторяется: у наклонного `Ø492*` звёздочка стоит выше и правее
    цифр и по отдельности не задевает ни одну из них — зато накрывает уже
    собранную рамку всей надписи. За один проход этого не увидеть.
    """
    for _ in range(MERGE_PASSES):
        merged = _merge_once(blocks, pitch)
        if len(merged) == len(blocks):
            return merged
        blocks = merged
    return blocks


def _merge_once(blocks: list[Block], pitch: float) -> list[Block]:
    tol = pitch * MERGE_GAP
    parent = list(range(len(blocks)))
    # Рамки, наложившиеся по обеим осям сразу, — точно одна надпись: у соседних
    # размеров такого не бывает. Для них ограничение по высоте не действует:
    # круто наклонённая надпись занимает больше трёх кеглей поперёк строки.
    solid = [True] * len(blocks)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, first in enumerate(blocks):
        for j in range(i + 1, len(blocks)):
            second = blocks[j]
            over_x = min(first.x + first.w, second.x + second.w) - max(first.x, second.x)
            over_y = min(first.y + first.h, second.y + second.h) - max(first.y, second.y)
            # Одного касания мало: рядом стоящие размеры тоже почти соприкасаются.
            # Нужно, чтобы по одной оси рамки честно перекрывались.
            if over_x < -tol or over_y < -tol or max(over_x, over_y) <= 0:
                continue
            # Угол должен совпадать. Пробовали склеивать и разноориентированные
            # куски — у `Ø492*` хвост `2*` детектор считает вертикальным, — но
            # тогда на «Колесе зубчатом» слиплись два соседних вертикальных
            # размера в один. Потерянная строка дороже лишней, правило осталось.
            if first.angle != second.angle:
                continue
            ra, rb = find(i), find(j)
            if ra != rb:
                parent[rb] = ra
            root = find(i)
            solid[root] = solid[root] and min(over_x, over_y) > 0

    families: dict[int, list[Block]] = {}
    for index, block in enumerate(blocks):
        families.setdefault(find(index), []).append(block)

    out: list[Block] = []
    for root, family in families.items():
        if len(family) == 1:
            out.append(family[0])
            continue
        x = min(b.x for b in family)
        y = min(b.y for b in family)
        w = max(b.x + b.w for b in family) - x
        h = max(b.y + b.h for b in family) - y
        along, cross = (w, h) if family[0].angle == 0 else (h, w)
        if along > pitch * MERGE_SPAN or (cross > pitch * MERGE_CROSS
                                          and not solid[root]):
            out.extend(family)          # склеилось через полчертежа — не надпись
            continue
        out.append(Block(id=0, x=x, y=y, w=w, h=h, angle=family[0].angle,
                         glyphs=sum(b.glyphs for b in family),
                         ink=sum(b.ink for b in family)))

    out.sort(key=lambda b: (b.y, b.x))
    for n, b in enumerate(out, 1):
        b.id = n
    return out


def build_blocks(glyphs: list[tuple], text_h: float,
                 limit: float | None = None) -> list[Block]:
    """Каждый глиф попадает в ту цепочку — горизонтальную или вертикальную, —
    где у него больше соседей. Одиночки остаются отдельными блоками (Ø, R, буквы видов).

    `limit` — предел длины цепочки. По умолчанию тридцать кеглей: этого хватало,
    пока кегль был занижен и строки распадались. С честным кеглем строка
    технических требований набирает и сорок кеглей — предел задаёт лист.
    """
    limit = limit or text_h * 30
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
        if max(w, h) > limit:
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
    # Первый проход нужен только затем, чтобы измерить кегль: сцепление символов
    # в строку зависит от него, а оценка по компонентам его занижает.
    pitch = kegel(build_blocks(glyphs, text_h), text_h)
    limit = max(pitch * 30, gray.shape[1] * CHAIN_SHARE)
    blocks = merge_overlapping(build_blocks(glyphs, pitch, limit), pitch)
    return {
        "image": str(path),
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
        "text_height": round(text_h, 1),
        "pitch": round(pitch, 1),
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
