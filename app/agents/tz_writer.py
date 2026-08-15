"""Модельный слой требований по изготовлению.

Код уже собрал состав, массы и материалы — это факты с чертежей, их модель
не трогает. Модель делает две вещи: формулирует «Общие данные» и восстанавливает
привычную запись технических требований там, где таблица канона не справилась.

Без ключа OpenRouter шаг пропускается: остаётся детерминированный черновик
из `tools/tz_canon.py`, документ собирается целиком.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.llm import LLM, LLMError, parse_json

ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT = ROOT / "app" / "prompts" / "tz.md"
RULES = [ROOT / ".claude" / "rules" / "tz-format.md",
         ROOT / ".claude" / "rules" / "glossary.md"]


def _system() -> str:
    parts = [PROMPT.read_text(encoding="utf-8")]
    for rule in RULES:
        if rule.exists():
            parts.append(f"\n\n--- {rule.name} ---\n" + rule.read_text(encoding="utf-8"))
    return "".join(parts)


def _digest(tz: dict) -> dict:
    """Во входные данные модели идёт только то, что ей нужно для решения."""
    tech: list[dict] = []
    nodes = [tz["assembly"]]
    while nodes:
        node = nodes.pop(0)
        nodes.extend(node.get("children", []))
        for holder in [node, *node.get("parts", [])]:
            for item in holder.get("tech_requirements", []):
                tech.append({"owner": holder["designation"], "no": item["no"],
                             "text": item["text"]})
    return {
        "designation": tz["designation"],
        "title": tz["title"],
        "mass": tz["mass"],
        "standard_items": sorted({
            row["name"] for node in _nodes(tz) for row in node.get("spec", [])
            if row.get("section") == "Стандартные изделия"}),
        "tech": tech,
    }


def _nodes(tz: dict) -> list[dict]:
    out, queue = [], [tz["assembly"]]
    while queue:
        node = queue.pop(0)
        out.append(node)
        queue.extend(node.get("children", []))
    return out


def _apply(tz: dict, answer: dict) -> None:
    fixes = {(item.get("owner"), str(item.get("no"))): item.get("text", "")
             for item in answer.get("tech", []) if item.get("text")}
    for node in _nodes(tz):
        for holder in [node, *node.get("parts", [])]:
            for item in holder.get("tech_requirements", []):
                new = fixes.get((holder["designation"], str(item["no"])))
                if new:
                    item["text"] = new
    if answer.get("general"):
        tz["general"] = list(answer["general"])
    tz["warnings"].extend(answer.get("warnings", []))


def write(tz: dict, llm: LLM | None = None) -> dict:
    try:
        llm = llm or LLM()
    except LLMError as error:
        tz["warnings"].append(
            f"«Общие данные» и правка формулировок пропущены: {error}")
        return tz

    user = json.dumps(_digest(tz), ensure_ascii=False, indent=1)
    try:
        answer = parse_json(llm.ask(_system(), user))
    except (LLMError, ValueError) as error:
        tz["warnings"].append(f"Модель не ответила разбираемым JSON: {error}")
        return tz

    if isinstance(answer, dict):
        _apply(tz, answer)
    else:
        tz["warnings"].append("Модель вернула не объект — правки пропущены")
    return tz
