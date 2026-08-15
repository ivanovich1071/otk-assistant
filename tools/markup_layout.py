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
NUMBERED_TT = re.compile(r"^\s*\d{1,2}\.\s")
HAS_DIGIT = re.compile(r"\d")

FRAME_WORDS = {
    "изм", "лист", "листов", "докум", "подп", "дата", "разраб", "пров",
    "контр", "утв", "масса", "масштаб", "формат", "копировал", "инв",
    "взам", "дубл", "справ", "перв", "примен", "лит",
}

# Порядок внутри группы: сначала диаметральные, потом линейные и угловые,
# потом радиусы, фаски и шероховатости.
TIER_DIAMETER, TIER_LINEAR, TIER_LAST = 0, 1, 2
RADIUS = re.compile(r"^R[\d.,]", re.IGNORECASE)
ROUGHNESS = re.compile(r"^R[az]\s", re.IGNORECASE)


def stamp_zone(width: int, height: int) -> tuple[float, float]:
    """Левая и верхняя границы основной надписи в пикселях."""
    return (width - (MARGIN_MM + STAMP_W_MM) * MM,
            height - (MARGIN_MM + STAMP_H_MM) * MM)


def classify(block: dict, text: str, width: int, height: int,
             tt_rows: set[int]) -> str:
    """Что это за надпись: размер, заголовок, штамп, техтребование или мусор."""
    value = (text or "").strip()
    if not value:
        return "junk"
    if VIEW_LETTER.match(value) or SECTION.match(value) or CALLOUT.match(value):
        return "caption"
    if block["y"] in tt_rows or NUMBERED_TT.match(value):
        return "tt"

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


def cluster(items: list[dict], text_height: float) -> list[list[dict]]:
    """Надписи одной проекции лежат рядом — режем по разрывам, а не по сетке."""
    gap = text_height * 14
    groups: list[list[dict]] = []
    for item in sorted(items, key=lambda i: (i["cx"], i["cy"])):
        for group in groups:
            if any(abs(item["cx"] - other["cx"]) < gap
                   and abs(item["cy"] - other["cy"]) < gap for other in group):
                group.append(item)
                break
        else:
            groups.append([item])

    # Слияние: кластеры, оказавшиеся соседями через третий, должны быть одним.
    merged = True
    while merged:
        merged = False
        for i, first in enumerate(groups):
            for second in groups[i + 1:]:
                if any(abs(a["cx"] - b["cx"]) < gap and abs(a["cy"] - b["cy"]) < gap
                       for a in first for b in second):
                    first.extend(second)
                    groups.remove(second)
                    merged = True
                    break
            if merged:
                break
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


def build(blocks: list[dict], texts: dict[int, dict], width: int, height: int,
          text_height: float, tt_rows: set[int] | None = None) -> dict:
    """Готовые группы с номерами и координатами."""
    tt_rows = tt_rows or set()
    sizes, captions, tech, skipped = [], [], [], []

    for block in blocks:
        read = texts.get(block["id"])
        value = (read or {}).get("text", "").strip()
        kind = classify(block, value, width, height, tt_rows)
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
