"""Сверка разметки с эталонной.

Сравниваются значения позиций и их распределение по группам. Номера позиций
не сравниваются напрямую: порядок внутри группы — вопрос правил, а не чтения,
и расхождение в нём не должно выглядеть как ошибка распознавания.

    python tools/compare_markup.py --markup work/markup.json --gold tests/gold/.../markup.json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

SHORT = {"отверстий": "отв.", "отверстия": "отв.", "отверстие": "отв.",
         "фаски": "фаски", "радиуса": "радиуса"}


def norm(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    for long, short in SHORT.items():
        text = text.replace(long, short)
    return text


def values(markup: dict) -> Counter:
    return Counter(norm(i["value"]) for g in markup["groups"] for i in g["items"])


def groups(markup: dict) -> list[str]:
    return [g["name"] for g in markup["groups"]]


def compare(made: dict, gold: dict) -> dict:
    ours, theirs = values(made), values(gold)
    missing = theirs - ours
    extra = ours - theirs
    return {
        "gold": sum(theirs.values()),
        "made": sum(ours.values()),
        "matched": sum((ours & theirs).values()),
        "missing": sorted(missing.elements()),
        "extra": sorted(extra.elements()),
        "groups_gold": groups(gold),
        "groups_made": groups(made),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Сверка разметки с эталоном")
    ap.add_argument("--markup", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    made = json.loads(args.markup.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    result = compare(made, gold)

    share = 100 * result["matched"] / max(1, result["gold"])
    print(f"позиций в эталоне: {result['gold']}, найдено: {result['made']}, "
          f"совпало: {result['matched']} ({share:.0f} %)")
    print(f"группы эталона: {', '.join(result['groups_gold'])}")
    print(f"группы разметки: {', '.join(result['groups_made'])}")
    if not args.quiet:
        for value in result["missing"]:
            print(f"  ! потеряно: {value}")
        for value in result["extra"]:
            print(f"  + лишнее:  {value}")


if __name__ == "__main__":
    main()
