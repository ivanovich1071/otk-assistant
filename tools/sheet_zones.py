"""Служебные зоны листа: рамка, таблицы, штамп, блок технических требований.

Всё это не размеры, и в карту обмера попадать не должно. На векторных чертежах
зоны берутся из текстового слоя (`vector_extract.py`), здесь то же самое для
растра — по маске длинных линий, которую и так строит `detect_text`.

Опорная точка — **рамка чертежа, а не край листа**: у сканов поля произвольные,
и отсчёт от угла листа промахивается на десятки миллиметров.

    python tools/sheet_zones.py "input/чертёж.png" --debug work/chk/zones.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_text import binarize, estimate_text_height, load_gray, remove_long_lines

MM = 300 / 25.4                 # пикселей в миллиметре при 300 dpi
STAMP_W_MM, STAMP_H_MM = 185.0, 55.0

FRAME_COVER = 0.55              # линия рамки тянется хотя бы на столько листа
MIN_CELL_MM = 4.0               # ячейка мельче — это не таблица, а шум
TABLE_ROWS = 3                  # столько ячеек друг над другом уже таблица
BORDER_BAND = 10                # в такой полосе вокруг края ищем линию рамки таблицы
TABLE_FILL = 0.55               # какую долю своей рамки должны занимать ячейки
OUTSIDE_REACH = 90              # насколько далеко за угол смотрим, не идёт ли линия дальше
DENSE_INK = 40                  # средняя чернота клетки, за которой начинается проекция
MIN_PROJECTION = 0.01           # проекция мельче этой доли рамки — не проекция
WHOLE_SHEET = 0.8               # сгусток крупнее этой доли листа — сама рамка
BIGGER_THAN_TABLE = 3.0         # проекция должна быть настолько крупнее таблицы


def _long_runs(mask: np.ndarray, axis: int, cover: float) -> list[int]:
    """Координаты линий, тянущихся вдоль всего листа."""
    counts = (mask > 0).sum(axis=axis)
    limit = mask.shape[axis] * cover
    hits = np.where(counts >= limit)[0]
    if not len(hits):
        return []
    # Толстая линия даёт несколько соседних координат — оставляем по одной.
    groups, start = [], hits[0]
    for previous, current in zip(hits, hits[1:]):
        if current - previous > 5:
            groups.append((start + previous) // 2)
            start = current
    groups.append((start + hits[-1]) // 2)
    return groups


def find_frame(lines: np.ndarray) -> tuple[int, int, int, int]:
    """Рамка чертежа: (x0, y0, x1, y1). Если не нашлась — весь лист."""
    height, width = lines.shape
    columns = _long_runs(lines, 0, FRAME_COVER)
    rows = _long_runs(lines, 1, FRAME_COVER)
    x0 = columns[0] if columns else 0
    x1 = columns[-1] if len(columns) > 1 else width - 1
    y0 = rows[0] if rows else 0
    y1 = rows[-1] if len(rows) > 1 else height - 1
    if x1 - x0 < width * 0.5 or y1 - y0 < height * 0.5:
        return 0, 0, width - 1, height - 1
    return int(x0), int(y0), int(x1), int(y1)


def find_tables(lines: np.ndarray, text_h: float) -> list[tuple[int, int, int, int]]:
    """Прямоугольники таблиц: параметров зацепления, спецификации, штампа.

    Ячейка таблицы — это дырка в маске линий. Несколько дырок, стоящих столбиком
    и совпадающих по ширине, — таблица.
    """
    closed = cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    holes = cv2.bitwise_not(closed)
    count, _, stats, _ = cv2.connectedComponentsWithStats(holes, 4)

    minimum = MIN_CELL_MM * MM
    cells = []
    for i in range(1, count):
        x, y, w, h = (stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP],
                      stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT])
        if w < minimum or h < minimum or h > text_h * 6:
            continue
        if stats[i, cv2.CC_STAT_AREA] < w * h * 0.6:
            continue                      # не прямоугольник, а клякса
        cells.append((x, y, w, h))

    # Ячейки одной таблицы стоят столбиком и совпадают по горизонтали.
    tables: list[list[tuple[int, int, int, int]]] = []
    for cell in sorted(cells, key=lambda c: (c[0], c[1])):
        for table in tables:
            if any(_overlap_x(cell, other) and _near_y(cell, other, text_h)
                   for other in table):
                table.append(cell)
                break
        else:
            tables.append([cell])

    out = []
    for table in tables:
        if len(table) < TABLE_ROWS:
            continue
        box = (int(min(c[0] for c in table)),
               int(min(c[1] for c in table)),
               int(max(c[0] + c[2] for c in table)),
               int(max(c[1] + c[3] for c in table)))
        # Ячейки настоящей таблицы замощают её целиком. Клетки, случайно
        # образованные контуром детали и размерными линиями, — нет: между ними
        # пустое поле чертежа. На «Колесе зубчатом» именно так отсекались
        # настоящие размеры 70 и 70 над разрезом.
        area = sum(c[2] * c[3] for c in table)
        if area < (box[2] - box[0]) * (box[3] - box[1]) * TABLE_FILL:
            continue
        if bordered(lines, box) and closed_corners(lines, box):
            out.append(box)
    return merge_touching(out)


def merge_touching(boxes: list[tuple[int, int, int, int]],
                   gap: int = 25) -> list[tuple[int, int, int, int]]:
    """Колонки одной таблицы находятся порознь — сшиваем их обратно.

    У таблицы параметров зацепления графы разной ширины, и ячейки правых колонок
    не попадают в один столбик с левыми. Без склейки правая часть таблицы
    оставалась снаружи зоны и её содержимое лезло в карту обмера.
    """
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        for i, first in enumerate(merged):
            for j in range(i + 1, len(merged)):
                second = merged[j]
                touch_x = first[0] - gap <= second[2] and second[0] - gap <= first[2]
                touch_y = first[1] - gap <= second[3] and second[1] - gap <= first[3]
                if touch_x and touch_y:
                    merged[i] = (min(first[0], second[0]), min(first[1], second[1]),
                                 max(first[2], second[2]), max(first[3], second[3]))
                    merged.pop(j)
                    changed = True
                    break
            if changed:
                break
    return merged


def closed_corners(lines: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """Линии рамки таблицы кончаются на углах, а выносные — идут дальше.

    Это и отличает таблицу от клеток, которые размерные линии нарезают над
    разрезом детали: там сетка заполнена не хуже настоящей таблицы, но линии
    прошивают её насквозь.
    """
    x0, y0, x1, y1 = box
    height, width = lines.shape
    reach = OUTSIDE_REACH

    def busy(a, b, c, d) -> float:
        piece = lines[max(0, a):min(height, b), max(0, c):min(width, d)]
        return 0.0 if piece.size == 0 else float((piece > 0).mean())

    outside = [
        busy(y0 - BORDER_BAND, y0 + BORDER_BAND, x0 - reach, x0),     # слева от верхней
        busy(y0 - BORDER_BAND, y0 + BORDER_BAND, x1, x1 + reach),     # справа от верхней
        busy(y0 - reach, y0, x0 - BORDER_BAND, x0 + BORDER_BAND),     # выше левой
        busy(y1, y1 + reach, x0 - BORDER_BAND, x0 + BORDER_BAND),     # ниже левой
    ]
    return sum(1 for share in outside if share > 0.25) <= 1


def bordered(lines: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """У настоящей таблицы обведены все четыре стороны.

    Без этой проверки в таблицы попадают клетки, случайно образованные линиями
    разреза и штриховки, — на «Колесе зубчатом» так отсекались настоящие размеры.
    """
    x0, y0, x1, y1 = box
    height, width = lines.shape
    band = BORDER_BAND

    def cut(a, b, c, d):
        return lines[max(0, a):min(height, b), max(0, c):min(width, d)]

    horizontal = [cut(y0 - band, y0 + band, x0, x1), cut(y1 - band, y1 + band, x0, x1)]
    vertical = [cut(y0, y1, x0 - band, x0 + band), cut(y0, y1, x1 - band, x1 + band)]

    for edge, axis in [(e, 0) for e in horizontal] + [(e, 1) for e in vertical]:
        if edge.size == 0 or (edge.max(axis=axis) > 0).mean() < 0.7:
            return False
    return True


def _overlap_x(a, b) -> bool:
    left, right = max(a[0], b[0]), min(a[0] + a[2], b[0] + b[2])
    return right - left > 0.4 * min(a[2], b[2])


def _near_y(a, b, text_h: float) -> bool:
    gap = min(abs(a[1] - (b[1] + b[3])), abs(b[1] - (a[1] + a[3])))
    return gap < text_h * 1.5


def find_projections(lines: np.ndarray, text_h: float,
                     frame: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    """Проекции детали — виды, разрезы, выноски.

    После удаления текста на листе остаются линии. Штриховка и контур детали
    лежат плотно, размерные линии — редко, поэтому плотные крупные сгустки
    и есть проекции. По ним же надписи разбиваются на группы: размер относится
    к той проекции, рядом с которой стоит, а не к соседней надписи.
    """
    # Рамку листа убираем: иначе весь лист слипается в одну «проекцию».
    body = lines.copy()
    x0, y0, x1, y1 = frame
    edge = max(6, int(text_h / 4))
    for a, b, c, d in ((y0 - edge, y0 + edge, 0, lines.shape[1]),
                       (y1 - edge, y1 + edge, 0, lines.shape[1]),
                       (0, lines.shape[0], x0 - edge, x0 + edge),
                       (0, lines.shape[0], x1 - edge, x1 + edge)):
        body[max(0, a):b, max(0, c):d] = 0

    step = max(8, int(text_h / 3))
    small = cv2.resize(body, (body.shape[1] // step, body.shape[0] // step),
                       interpolation=cv2.INTER_AREA)
    dense = (small > DENSE_INK).astype(np.uint8) * 255
    dense = cv2.morphologyEx(dense, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(dense, 8)
    x0, y0, x1, y1 = frame
    sheet = (x1 - x0) * (y1 - y0)
    least = sheet / (step * step) * MIN_PROJECTION
    out = []
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] < least:
            continue
        spread = (stats[i, cv2.CC_STAT_WIDTH] * stats[i, cv2.CC_STAT_HEIGHT]
                  * step * step)
        if spread > sheet * WHOLE_SHEET:
            continue          # это сама рамка листа, а не проекция детали
        out.append((int(stats[i, cv2.CC_STAT_LEFT] * step),
                    int(stats[i, cv2.CC_STAT_TOP] * step),
                    int((stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH]) * step),
                    int((stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]) * step)))
    return out


def _covers(big: tuple[int, int, int, int], small: tuple[int, int, int, int]) -> bool:
    """Кандидат в таблицы сидит на проекции — и проекция заметно крупнее его.

    Условие на размер обязательно: иначе таблица параметров, которую детектор
    видит и как плотный сгусток, забраковала бы сама себя.
    """
    if big[2] <= small[0] or small[2] <= big[0] or big[3] <= small[1] or small[3] <= big[1]:
        return False
    area = (big[2] - big[0]) * (big[3] - big[1])
    return area > (small[2] - small[0]) * (small[3] - small[1]) * BIGGER_THAN_TABLE


def stamp_rect(frame: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Основная надпись по ГОСТ 2.104 — в правом нижнем углу рамки."""
    _, _, x1, y1 = frame
    return (int(x1 - STAMP_W_MM * MM), int(y1 - STAMP_H_MM * MM), int(x1), int(y1))


def analyze(gray: np.ndarray) -> dict:
    bw = binarize(gray)
    text_h = estimate_text_height(bw)
    _, lines = remove_long_lines(bw, max(24, int(text_h * 2.2)))
    frame = find_frame(lines)
    projections = find_projections(lines, text_h, frame)
    stamp = stamp_rect(frame)
    # Таблицу никогда не рисуют поверх детали: кандидат, севший на проекцию, —
    # это клетки, нарезанные размерными линиями, а не таблица.
    tables = [t for t in find_tables(lines, text_h)
              if not any(_covers(p, t) for p in projections)]
    return {
        "text_height": round(text_h, 1),
        "frame": list(frame),
        "stamp": list(stamp),
        "tables": [list(t) for t in tables],
        "projections": [list(p) for p in projections],
    }


def inside(box: tuple[int, int, int, int], zone: list[int], part: float = 0.6) -> bool:
    """Надпись считается внутри зоны, если в неё попала большая часть её площади."""
    x, y, w, h = box
    left, top = max(x, zone[0]), max(y, zone[1])
    right, bottom = min(x + w, zone[2]), min(y + h, zone[3])
    if right <= left or bottom <= top:
        return False
    return (right - left) * (bottom - top) >= w * h * part


def draw_debug(gray: np.ndarray, zones: dict, out: Path) -> None:
    canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    x0, y0, x1, y1 = zones["frame"]
    cv2.rectangle(canvas, (x0, y0), (x1, y1), (255, 0, 0), 4)
    for table in zones["tables"]:
        cv2.rectangle(canvas, (table[0], table[1]), (table[2], table[3]), (0, 160, 0), 4)
    s = zones["stamp"]
    cv2.rectangle(canvas, (s[0], s[1]), (s[2], s[3]), (0, 0, 255), 4)
    ok, buf = cv2.imencode(".png", canvas)
    if ok:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(buf.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description="Служебные зоны листа")
    ap.add_argument("image", type=Path)
    ap.add_argument("--debug", type=Path)
    args = ap.parse_args()

    gray = load_gray(args.image)
    zones = analyze(gray)
    print(json.dumps(zones, ensure_ascii=False))
    print(f"рамка {zones['frame']}, таблиц {len(zones['tables'])}")
    if args.debug:
        draw_debug(gray, zones, args.debug)
        print(f"-> {args.debug}")


if __name__ == "__main__":
    main()
