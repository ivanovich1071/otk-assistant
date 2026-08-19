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
EXTRA_W_MM, EXTRA_H_MM = 75.0, 18.0
MARGIN_OUT_MM, MARGIN_IN_MM = 12.0, 3.0
# Поля рамки по ГОСТ 2.301: 20 мм под подшивку слева, 5 мм с трёх сторон.
FIELD_LEFT_MM, FIELD_MM = 20.0, 5.0
FRAME_TOLERANCE_MM = 10.0       # дальше от норматива — находка не рамка, а помеха

FRAME_COVER = 0.55              # линия рамки тянется хотя бы на столько листа
FRAME_BRIDGE_MM = 40.0          # разрыв в выцветшей линии рамки, который сшиваем
SPEC_MIN_LINES = 3              # столько разделителей колонок делают спецификацию
SPEC_MIN_ROWS = 3               # столько строк подряд над штампом — уже таблица
SPEC_ROW_COVER = 0.6            # какую долю полосы занимает линия строки
SPEC_ROW_GAP = 3.0              # разрыв между строками таблицы, в кеглях
SPEC_MIN_H_MM = 15.0            # полоса ниже этой — не спецификация, а линия штампа
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


def gost_frame(width: int, height: int) -> tuple[int, int, int, int]:
    """Рамка по ГОСТ 2.301, отсчитанная от края листа.

    Лист мы рендерим из PDF сами и знаем, что 300 dpi — значит страница и есть
    формат. Это опора на случай, когда линию рамки на скане не видно.
    """
    return (int(FIELD_LEFT_MM * MM), int(FIELD_MM * MM),
            int(width - 1 - FIELD_MM * MM), int(height - 1 - FIELD_MM * MM))


def find_frame(lines: np.ndarray) -> tuple[int, int, int, int]:
    """Рамка чертежа: (x0, y0, x1, y1).

    Линию рамки ищем по маске, замкнутой вдоль своей оси: на выцветшем скане
    она рвётся, и ни один столбец пикселей не набирает нужного покрытия.
    На «Колесе тяговом» левая линия сплошная только в верхней половине листа —
    35 % при пороге 55 %, — и рамкой оказывался весь лист вместе с полями.
    Найденное сверяем с ГОСТ 2.301: чего не видно, берём по нормативу.
    """
    height, width = lines.shape
    bridge = max(3, int(FRAME_BRIDGE_MM * MM))
    columns = _long_runs(
        cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((bridge, 1), np.uint8)),
        0, FRAME_COVER)
    rows = _long_runs(
        cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((1, bridge), np.uint8)),
        1, FRAME_COVER)

    fallback = gost_frame(width, height)
    found = (columns[0] if columns else None,
             rows[0] if rows else None,
             columns[-1] if len(columns) > 1 else None,
             rows[-1] if len(rows) > 1 else None)

    limit = FRAME_TOLERANCE_MM * MM
    frame = tuple(int(value) if value is not None and abs(value - default) <= limit
                  else default
                  for value, default in zip(found, fallback))
    if frame[2] - frame[0] < width * 0.5 or frame[3] - frame[1] < height * 0.5:
        return fallback
    return frame


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


def extra_rects(frame: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    """Графы обозначения по ГОСТ 2.104 — верхние углы рамки.

    Обозначение чертежа повторяют в дополнительной графе: на горизонтальном
    листе — в левом верхнем углу, повёрнутым на 180°; на вертикальном —
    в правом верхнем, повёрнутым на 90°. Детектор читает их как обычные
    надписи: на «Станине» так уходили в карту обрывки `KBZ536-1`, на «Колесе
    тяговом» — `1-3653.00СБ` и `501-.`.

    Ориентацию листа не угадываем — закрываем оба угла: графа занимает
    считанные сантиметры, и настоящих размеров там не ставят.
    """
    x0, y0, x1, _ = frame
    return [
        (int(x0), int(y0), int(x0 + EXTRA_W_MM * MM), int(y0 + EXTRA_H_MM * MM)),
        (int(x1 - EXTRA_H_MM * MM), int(y0), int(x1), int(y0 + EXTRA_W_MM * MM)),
    ]


def margin_rect(frame: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Колонка дополнительных граф вдоль левого края рамки.

    «Инв. № подл.», «Подп. и дата», «Взам. инв. №» — по ГОСТ 2.104 они стоят
    полосой шириной 12 мм и написаны боком. Полоса лежит слева от линии рамки,
    но значения граф на неё наползают, поэтому проверкой «надпись вне рамки»
    они не отсекались: `001160` приходило в карту позицией.

    Внутрь рамки полоса заходит всего на 3 мм: на «Станине» размер `15,5` стоит
    в 13 мм от рамки, и полоса пошире съедала бы его. Вдобавок отсекается только
    повёрнутая надпись — графы пишут боком, размеры нет.
    """
    x0, y0, _, y1 = frame
    return (int(x0 - MARGIN_OUT_MM * MM), int(y0),
            int(x0 + MARGIN_IN_MM * MM), int(y1))


def _spec_by_rows(band: np.ndarray, pitch: float) -> int | None:
    """Верх спецификации по горизонтальным линиям строк.

    Строки таблицы идут ровной стопкой прямо на штампе. Считаем линией ряд,
    заполненный чернилами почти на всю ширину полосы, и поднимаемся от штампа
    вверх, пока следующая линия недалеко.
    """
    wide = cv2.morphologyEx(band, cv2.MORPH_CLOSE,
                            np.ones((1, max(3, int(pitch * 1.5))), np.uint8))
    rows = np.where((wide > 0).mean(axis=1) >= SPEC_ROW_COVER)[0]
    if not len(rows):
        return None

    levels: list[int] = []
    for y in rows:
        if levels and y - levels[-1] <= 5:
            continue                       # толстая линия — тот же ряд
        levels.append(int(y))

    top, kept = band.shape[0], 0
    for y in reversed(levels):
        if top - y > pitch * SPEC_ROW_GAP:
            break
        top, kept = y, kept + 1
    return top if kept >= SPEC_MIN_ROWS else None


def _spec_by_dividers(band: np.ndarray, pitch: float) -> int | None:
    """Верх спецификации по вертикальным разделителям колонок.

    Запасной путь для сканов, где горизонтальные линии строк выцвели: на «Колесе
    тяговом» их видно две из десяти, зато семь разделителей колонок целы и все
    обрываются на одном уровне. Берём уровень, до которого дотянулись хотя бы
    три разделителя, — одиночная линия бывает и краем чертежа.
    """
    # Скан «ведёт»: линия длиной в сантиметры уходит вбок на несколько пикселей,
    # и по одному столбцу она не читается. Размазываем поперёк.
    band = cv2.dilate(band, np.ones((1, max(3, int(pitch * 0.3))), np.uint8))
    # Верх штампа посчитан по ГОСТ от рамки и на пару миллиметров не совпадает
    # с нарисованным. Подставляем снизу полоску «чернил», чтобы разделитель,
    # оборвавшийся чуть выше, всё равно считался доходящим до штампа.
    tol = int(pitch * 2)
    band = np.vstack([band, np.full((tol, band.shape[1]), 255, np.uint8)])
    bridge = max(3, int(pitch * 1.5))
    closed = cv2.morphologyEx(band, cv2.MORPH_CLOSE, np.ones((bridge, 1), np.uint8)) > 0

    # Длина сплошного хвоста в каждом столбце: разворачиваем и ищем первый разрыв.
    flipped = closed[::-1]
    runs = np.argmax(~flipped, axis=0)
    runs = np.where(flipped.all(axis=0), flipped.shape[0], runs) - tol

    hits = np.where(runs >= SPEC_MIN_H_MM * MM)[0]
    if not len(hits):
        return None

    tops: list[int] = []
    column: list[int] = []
    for i in hits:
        if column and i - column[-1] > pitch:
            tops.append(int(runs[column].max()))
            column = []
        column.append(i)
    tops.append(int(runs[column].max()))

    if len(tops) < SPEC_MIN_LINES:
        return None
    tops.sort(reverse=True)
    return int(band.shape[0] - tol - tops[SPEC_MIN_LINES - 1])


def spec_rect(lines: np.ndarray, stamp: tuple[int, int, int, int],
              pitch: float) -> tuple[int, int, int, int] | None:
    """Спецификация на листе — полоса над штампом той же ширины.

    По ГОСТ 2.106 её графы повторяют ширину основной надписи, 185 мм, и стоят
    прямо на ней. Сеткой ячеек её не находит `find_tables`: на скане рвутся
    то горизонтальные линии строк, то вертикальные разделители колонок. Поэтому
    ищем двумя способами и верим тому, который нашёл структуру, а не обрывок.
    """
    x0, stamp_top, x1, _ = stamp
    band = lines[:max(0, stamp_top), max(0, x0):x1]
    if band.size == 0:
        return None

    top = _spec_by_rows(band, pitch)
    if top is None:
        top = _spec_by_dividers(band, pitch)
    if top is None or stamp_top - top < SPEC_MIN_H_MM * MM:
        return None
    return (int(x0), int(top), int(x1), int(stamp_top))


def extra_rect(frame: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Совместимость: первый из прямоугольников `extra_rects`."""
    return extra_rects(frame)[0]


def leader_ratios(lines: np.ndarray, blocks: list[dict],
                  pitch: float) -> dict[int, float]:
    """Во сколько раз линия под надписью длиннее самой надписи.

    Номер позиции по ГОСТ 2.109 стоит на полке-выноске — коротком отрезке чуть
    шире самого номера. Размерное число стоит на размерной линии, которая тянется
    на всю измеряемую длину. Это их и разделяет: полка около двух ширин,
    размерная линия — от трёх и больше.

    Надписи без линии под ними в ответ не попадают: у них признака нет.
    """
    height, width = lines.shape
    reach = max(4, int(pitch * 0.8))
    out: dict[int, float] = {}
    for block in blocks:
        if block["angle"] != 0:
            continue
        top = min(height - 1, block["y"] + block["h"])
        band = lines[top:min(height, top + reach), :]
        if band.size == 0:
            continue
        centre = block["x"] + block["w"] // 2
        # Мерить надо одну строку пикселей, а не всю полосу: полки соседних
        # выносок стоят на разной высоте, и объединение по полосе сшивало их
        # в одну длинную линию — номера позиций 67…71 проходили как размеры.
        row = next((r for r in range(band.shape[0])
                    if band[r, max(0, centre - 3):centre + 4].any()), None)
        if row is None:
            continue
        profile = (band[max(0, row - 1):row + 2] > 0).any(axis=0)
        left = right = centre
        while left > 0 and profile[left - 1]:
            left -= 1
        while right < width - 1 and profile[right + 1]:
            right += 1
        # Делим на ширину надписи, но не меньше кегля: одиночная «1» вдвое уже
        # двузначного номера, и на той же полке давала вдвое больший отношение —
        # позиция 1 на «Колесе тяговом» так и уходила в карту размером.
        out[block["id"]] = round((right - left) / max(block["w"], pitch, 1), 2)
    return out


def analyze(gray: np.ndarray, blocks: list[dict] | None = None,
            pitch: float | None = None) -> dict:
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
    spec = spec_rect(lines, stamp, pitch or text_h)
    result = {
        "text_height": round(text_h, 1),
        "frame": list(frame),
        "stamp": list(stamp),
        "extra": [list(e) for e in extra_rects(frame)],
        "margin": list(margin_rect(frame)),
        "spec": list(spec) if spec else None,
        "tables": [list(t) for t in tables],
        "projections": [list(p) for p in projections],
    }
    if blocks:
        result["leader"] = leader_ratios(lines, blocks, pitch or text_h)
    return result


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
    boxes = [zones["stamp"], *zones.get("extra", [])]
    if zones.get("spec"):
        boxes.append(zones["spec"])
    for s in boxes:
        cv2.rectangle(canvas, (s[0], s[1]), (s[2], s[3]), (0, 0, 255), 4)
    for box in zones.get("text", []):
        cv2.rectangle(canvas, (box[0], box[1]), (box[2], box[3]), (200, 0, 200), 4)
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
    print(f"рамка {zones['frame']}, таблиц {len(zones['tables'])}, "
          f"спецификация {zones['spec']}")
    if args.debug:
        draw_debug(gray, zones, args.debug)
        print(f"-> {args.debug}")


if __name__ == "__main__":
    main()
