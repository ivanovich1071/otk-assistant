"""Разбиение прочитанных надписей на группы и нумерация — без модели.

Модель однажды уже попробовали просить сделать это одним запросом: она потратила
24 000 токенов на рассуждения и вернула пустой ответ. Задача геометрическая,
и правила её описаны в `numbering.md` однозначно, поэтому считает код.

Модель остаётся тем, чем была: глазами. Она читает надписи, а не решает,
где кончается один вид и начинается другой.
"""
from __future__ import annotations

import re

DPI = 300
MM = DPI / 25.4                     # пикселей в миллиметре

STAMP_W_MM, STAMP_H_MM = 185.0, 60.0
MARGIN_MM = 5.0

# Заголовки проекций: буква без масштаба — вид, с масштабом — выноска,
# «А-А» — разрез (см. numbering.md).
VIEW_LETTER = re.compile(r"^[А-ЯA-Z]$")
SECTION = re.compile(r"^([А-ЯA-Z])\s*-\s*\1$")
CALLOUT = re.compile(r"^([А-ЯA-Z])\s*\(\s*\d+\s*:\s*[\d,]+\s*\)$")
# Детектор часто режет номер пункта в отдельный блок, поэтому после точки
# может не быть ничего: «1.» — это тоже начало технического требования.
NUMBERED_TT = re.compile(r"^\s*\d{1,2}\.(\s|$)")
HAS_DIGIT = re.compile(r"\d")

FRAME_WORDS = {
    "изм", "лист", "листов", "докум", "подп", "дата", "разраб", "пров",
    "контр", "утв", "масса", "масштаб", "формат", "копировал", "инв",
    "взам", "дубл", "справ", "перв", "примен", "лит",
}

# Порядок внутри группы: сначала диаметральные, потом линейные и угловые,
# потом радиусы, фаски и шероховатости.
CORRIDOR = 8.0                  # коридор пустоты шире стольких кеглей делит проекции
MAX_GROUPS = 3                  # больше трёх проекций на листе почти не бывает
MIN_IN_GROUP = 3                # группа из одного-двух размеров — это промах, а не вид
TIER_DIAMETER, TIER_LINEAR, TIER_LAST = 0, 1, 2
RADIUS = re.compile(r"^R[\d.,]", re.IGNORECASE)
ROUGHNESS = re.compile(r"^R[az]\s", re.IGNORECASE)


def stamp_zone(width: int, height: int) -> tuple[float, float]:
    """Левая и верхняя границы основной надписи в пикселях."""
    return (width - (MARGIN_MM + STAMP_W_MM) * MM,
            height - (MARGIN_MM + STAMP_H_MM) * MM)


def classify(block: dict, text: str, width: int, height: int,
             tt_rows: set[int], zones: dict | None = None) -> str:
    """Что это за надпись: размер, заголовок, штамп, техтребование или мусор."""
    value = (text or "").strip()
    if not value:
        return "junk"
    if VIEW_LETTER.match(value) or SECTION.match(value) or CALLOUT.match(value):
        return "caption"
    if block["y"] in tt_rows or NUMBERED_TT.match(value):
        return "tt"

    box = (block["x"], block["y"], block["w"], block["h"])
    if zones:
        # Зоны найдены по линиям листа: штамп отсчитан от рамки чертежа, а не от
        # края листа, поэтому на сканах с произвольными полями не промахивается.
        import sheet_zones                          # noqa: PLC0415
        if sheet_zones.inside(box, zones["stamp"]):
            return "stamp"
        if any(sheet_zones.inside(box, table) for table in zones["tables"]):
            return "table"
        if not sheet_zones.inside(box, zones["frame"], part=0.5):
            return "frame"
    else:
        stamp_x, stamp_y = stamp_zone(width, height)
        if block["x"] + block["w"] > stamp_x and block["y"] + block["h"] > stamp_y:
            return "stamp"
    if block["x"] < 15 * MM or block["y"] < 8 * MM:
        return "frame"                      # колонки «Инв. № подл.» и верхний угол
    words = re.findall(r"[А-Яа-яA-Za-z]+", value.lower())
    if words and all(w[:6] in FRAME_WORDS or w in FRAME_WORDS for w in words):
        return "frame"
    if not HAS_DIGIT.search(value):
        return "text"
    return "size"


def _in_box(block: dict, box: tuple[int, int, int, int]) -> bool:
    cx, cy = block["x"] + block["w"] / 2, block["y"] + block["h"] / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _corridor(items: list[dict], key: str, text_height: float) -> tuple[float, float]:
    """Самый широкий коридор пустоты по одной оси: (ширина, где резать)."""
    edges = sorted(((i[key] - (i["w"] if key == "cx" else i["h"]) / 2,
                     i[key] + (i["w"] if key == "cx" else i["h"]) / 2) for i in items),
                   key=lambda e: e[0])
    best, cut, reach = 0.0, 0.0, edges[0][1]
    for start, end in edges[1:]:
        if start - reach > best:
            best, cut = start - reach, (reach + start) / 2
        reach = max(reach, end)
    return best, cut


def cluster(items: list[dict], text_height: float) -> list[list[dict]]:
    """Проекции разделены пустотой — режем лист по самому широкому коридору.

    Склейка соседей по расстоянию не работала: у одной проекции размеры стоят
    ярусами далеко друг от друга, а у соседней — вплотную, и главный вид
    разваливался на куски, слипаясь при этом с текстом рядом.
    """
    if len(items) < 2:
        return [items] if items else []

    limit = text_height * CORRIDOR
    groups = [items]
    # Резать до упора нельзя: у одной проекции размеры стоят ярусами и разрывы
    # между ними не уже, чем между самими проекциями. Поэтому делим ограниченное
    # число раз и каждый раз — по самому широкому коридору на листе.
    while len(groups) < MAX_GROUPS:
        best = None
        for index, group in enumerate(groups):
            if len(group) < 2:
                continue
            for key in ("cx", "cy"):
                width, cut = _corridor(group, key, text_height)
                if width >= limit and (best is None or width > best[0]):
                    best = (width, index, key, cut)
        if best is None:
            break
        _, index, key, cut = best
        group = groups[index]
        left = [i for i in group if i[key] < cut]
        right = [i for i in group if i[key] >= cut]
        # Отколовшаяся пара надписей — не проекция, а промах: лучше оставить
        # одну честную группу, чем плодить «виды» из одного размера.
        if min(len(left), len(right)) < MIN_IN_GROUP:
            break
        groups.pop(index)
        groups.extend((left, right))
    return [g for g in groups if g]


def name_groups(groups: list[list[dict]], captions: list[dict]) -> list[str]:
    """Названия по ГОСТ 2.305: проекция справа от главного вида — «Вид слева»."""
    if not groups:
        return []
    centres = [(sum(i["cx"] for i in g) / len(g), sum(i["cy"] for i in g) / len(g))
               for g in groups]
    main = max(range(len(groups)), key=lambda i: len(groups[i]))
    main_x, main_y = centres[main]

    names = []
    for index, (cx, cy) in enumerate(centres):
        caption = _caption_for(cx, cy, captions)
        # Над главным видом буквы не ставят: одиночная `R` или `А` рядом с ним —
        # это часть размера или обозначение поверхности, а не заголовок вида.
        if index == main and caption.startswith("Вид "):
            caption = ""
        if caption:
            names.append(caption)
        elif index == main:
            names.append("Главный вид")
        elif abs(cx - main_x) > abs(cy - main_y):
            names.append("Вид слева" if cx > main_x else "Вид справа")
        else:
            names.append("Вид сверху" if cy > main_y else "Вид снизу")
    return names


def _caption_for(cx: float, cy: float, captions: list[dict]) -> str:
    near = [c for c in captions
            if abs(c["cx"] - cx) < 900 and c["cy"] < cy]
    if not near:
        return ""
    caption = min(near, key=lambda c: cy - c["cy"])
    value = caption["text"].strip()
    if SECTION.match(value):
        return f"Разрез {value}"
    if match := CALLOUT.match(value):
        return f"Выноска {match.group(1)}"
    if VIEW_LETTER.match(value):
        return f"Вид {value}"
    return ""


def _tier(value: str) -> int:
    if value.startswith("Ø"):
        return TIER_DIAMETER
    if RADIUS.match(value) or ROUGHNESS.match(value) or "×" in value:
        return TIER_LAST
    return TIER_LINEAR


def order(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (_tier(i["value"]), i["x"], i["y"]))


def tech_zone(blocks: list[dict], texts: dict[int, dict],
              text_height: float) -> tuple[int, int, int, int] | None:
    """Область технических требований целиком, а не только нумерованные строки.

    Пункт `4. Неуказанные предельные отклонения размеров:` продолжается двумя
    строками без номера — без захвата области они уходили в карту как размеры.
    Опора — колонка, в которой стоят номера пунктов: текст ТТ прижат к ней слева.
    """
    starts = [b for b in blocks
              if NUMBERED_TT.match((texts.get(b["id"]) or {}).get("text", "").strip())]
    if len(starts) < 2:
        return None

    left = min(b["x"] for b in starts)
    top = min(b["y"] for b in starts)

    # Текст требований прижат к одной колонке. Идём вниз от первого пункта,
    # пока строки идут подряд: разрыв больше двух строк — блок кончился.
    column = [b for b in blocks
              if b["x"] > left - text_height * 2 and b["y"] + b["h"] > top]
    rows: list[list[dict]] = []
    for block in sorted(column, key=lambda b: b["y"]):
        if rows and block["y"] - max(x["y"] for x in rows[-1]) <= text_height * 1.2:
            rows[-1].append(block)
        else:
            rows.append([block])

    kept = [rows[0]]
    for previous, current in zip(rows, rows[1:]):
        if min(b["y"] for b in current) - max(b["y"] + b["h"] for b in previous) \
                > text_height * 2.5:
            break
        kept.append(current)

    flat = [b for row in kept for b in row]
    return (int(left - text_height), int(top - text_height * 0.5),
            int(max(b["x"] + b["w"] for b in flat) + text_height),
            int(max(b["y"] + b["h"] for b in flat) + text_height * 0.5))


def build(blocks: list[dict], texts: dict[int, dict], width: int, height: int,
          text_height: float, tt_rows: set[int] | None = None,
          zones: dict | None = None) -> dict:
    """Готовые группы с номерами и координатами."""
    tt_rows = tt_rows or set()
    sizes, captions, tech, skipped = [], [], [], []
    tt_box = tech_zone(blocks, texts, text_height)

    for block in blocks:
        read = texts.get(block["id"])
        value = (read or {}).get("text", "").strip()
        kind = classify(block, value, width, height, tt_rows, zones)
        if kind == "size" and tt_box and _in_box(block, tt_box):
            kind = "tt"
        entry = {"id": block["id"], "value": value,
                 "cx": block["x"] + block["w"] / 2, "cy": block["y"] + block["h"] / 2,
                 "x": block["x"], "y": block["y"], "w": block["w"], "h": block["h"],
                 "text": value, "sure": (read or {}).get("sure", True)}
        if kind == "size":
            sizes.append(entry)
        elif kind == "caption":
            captions.append(entry)
        elif kind == "tt":
            tech.append(entry)
        elif kind != "junk":
            skipped.append({"block": block["id"], "reason": kind})

    groups = cluster(sizes, text_height)
    names = name_groups(groups, captions)
    order_index = sorted(range(len(groups)),
                         key=lambda i: (sum(x["cy"] for x in groups[i]) / len(groups[i]) // 1200,
                                        sum(x["cx"] for x in groups[i]) / len(groups[i])))

    result, number = [], 1
    for index in order_index:
        items = []
        for entry in order(groups[index]):
            items.append({
                "no": str(number), "value": entry["value"], "kind": "text",
                "block": entry["id"], "blocks": [entry["id"]],
                "x": entry["x"], "y": entry["y"], "w": entry["w"], "h": entry["h"],
                "label_x": None, "label_y": None,
                "confidence": "high" if entry["sure"] else "low",
            })
            number += 1
        result.append({"name": names[index], "items": items})

    return {
        "groups": result,
        "tech_requirements": [{"no": "", "text": e["value"]}
                              for e in sorted(tech, key=lambda e: e["cy"])],
        "skipped": skipped,
    }
