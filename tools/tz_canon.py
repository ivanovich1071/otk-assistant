"""Канон технических требований и раздела «Общие данные».

Двоеточия, точки с запятой и дроби на чертеже нередко нарисованы графикой,
а не набраны текстом, поэтому из PDF приходит «H14, h14, ± IT14 2 .».
Таблица ниже восстанавливает то, что на чертеже нарисовано, — и ничего сверх.

Правила описаны в `.claude/rules/tz-format.md` и правятся текстом.
"""
from __future__ import annotations

import re

# (что искать, чем заменить) — применяются по порядку к тексту одного пункта.
RULES: list[tuple[str, str]] = [
    (r'"([^"]+)"', "«\\1»"),
    (r"«Литол-24»\s*\.\s*ГОСТ", "«Литол-24» ГОСТ"),
    (r"^\*?\s*(Размеры?)\s+для\s+справок\.?$", "* \\1 для справок."),
    (r"^\s*[-–—]\s*места\s+строповки\.?$", "◙ Места строповки."),
    (r"Общие\s+допуски\s+по\s+ГОСТ\s+30893\.1-2002\s*[;:]?\s*"
     r"H(\d+)\s*[,;]\s*h(\d+)\s*[,;]\s*±\s*IT(\d+)\s*/?\s*2\s*\.?",
     "Общие допуски по ГОСТ 30893.1-2002: H\\1; h\\2; ±IT\\3/2."),
    (r"Термообработка:\s*улучшение,?\s*НВ\s*=?\s*(\d+)\s*[-–—…]+\s*(\d+)\s*\.?",
     "Термообработка: улучшение, НВ = \\1…\\2."),
    (r"^(Длина\s+развёртки)\s+(?![–—-])", "\\1 – "),
    (r"^(Размер\s+в\s+скобках)\s+(?![–—-])", "\\1 – "),
]

_COMPILED = [(re.compile(pattern), replacement) for pattern, replacement in RULES]

_BEARING = re.compile(r"^Подшипник\s+(\S+)\s+(ГОСТ\s+\S+)")
_SIZE = re.compile(r"(\d{2,3})\.(\d{1,3})[Фф]?-(\d+)")


def canon(text: str) -> str:
    """Один пункт технических требований в каноническом виде."""
    result = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _COMPILED:
        result = pattern.sub(replacement, result)
    if result and result[-1] not in ".:;":
        result += "."
    return result


def _standard_items(node: dict) -> list[str]:
    return [row["name"] for row in node.get("spec", [])
            if row.get("section") == "Стандартные изделия"]


def _bearings(assembly: dict) -> str:
    """Тип подшипникового узла — из раздела «Стандартные изделия» спецификаций."""
    nodes = [assembly]
    while nodes:
        node = nodes.pop(0)
        nodes.extend(node.get("children", []))
        for name in _standard_items(node):
            match = _BEARING.match(name)
            if match:
                return f"Подшипниковые узлы – тип {match.group(1)} {match.group(2)};"
    return ""


def general(tz: dict) -> list[str]:
    """Черновик «Общих данных» без модели: то, что выводится однозначно."""
    assembly = tz["assembly"]
    title = tz.get("title", "")
    name = _SIZE.sub("", title).strip(" -—–").strip()

    lines = []
    if name:
        lines.append(f"{name};")
    bearings = _bearings(assembly)
    if bearings:
        lines.append(bearings)
    if tz.get("mass"):
        lines.append(f"Общая масса сборки – {tz['mass']}.")
    return lines


def apply(tz: dict) -> dict:
    """Канон по всему дереву; «Общие данные» заполняются, если ещё пусты."""
    nodes = [tz["assembly"]]
    while nodes:
        node = nodes.pop(0)
        nodes.extend(node.get("children", []))
        for holder in [node, *node.get("parts", [])]:
            for item in holder.get("tech_requirements", []):
                item["text"] = canon(item["text"])
    if not tz.get("general"):
        tz["general"] = general(tz)
    return tz
