"""Формальная проверка work/markup.json.

Ловит то, что видно без чертежа: дубли номеров, разрывы сквозной нумерации,
подномера без основного номера, пустые значения, битые координаты, позиции
с непокрытыми блоками.

    python tools/check_markup.py work/markup.json [--blocks work/blocks.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


NO_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def check(markup: dict, blocks: dict | None) -> list[str]:
    problems: list[str] = []
    items = [(g["name"], it) for g in markup.get("groups", []) for it in g.get("items", [])]

    if not items:
        return ["в разметке нет ни одной позиции"]

    seen: dict[str, str] = {}
    bases: set[int] = set()
    order: list[tuple[int, int]] = []

    for group, it in items:
        no = str(it.get("no", "")).strip()
        m = NO_RE.match(no)
        if not m:
            problems.append(f"поз. «{no}» ({group}): номер не в формате N или N-M")
            continue
        if no in seen:
            problems.append(f"поз. {no}: дубль — уже есть в группе «{seen[no]}»")
        seen[no] = group
        base, sub = int(m.group(1)), int(m.group(2) or 0)
        order.append((base, sub))
        if sub == 0:
            bases.add(base)

        if it.get("kind") == "frame":
            pass  # у рамки значение может быть пустым — она идёт картинкой
        elif not str(it.get("value", "")).strip():
            problems.append(f"поз. {no} ({group}): пустое значение")

        for key in ("x", "y", "w", "h"):
            if not isinstance(it.get(key), int) or it.get(key, -1) < 0:
                problems.append(f"поз. {no}: некорректная координата {key}={it.get(key)!r}")
                break
        if it.get("w", 1) == 0 or it.get("h", 1) == 0:
            problems.append(f"поз. {no}: нулевой размер рамки")

    for base, sub in order:
        if sub and base not in bases:
            problems.append(f"поз. {base}-{sub}: нет основного номера {base}")

    if bases:
        expected = set(range(1, max(bases) + 1))
        gaps = sorted(expected - bases)
        if gaps:
            problems.append(f"разрывы сквозной нумерации: {', '.join(map(str, gaps))}")

    if order != sorted(order):
        wrong = [f"{b}-{s}" if s else str(b)
                 for (b, s), (eb, es) in zip(order, sorted(order)) if (b, s) != (eb, es)]
        problems.append(f"порядок позиций нарушен, начиная с: {', '.join(wrong[:5])}")

    if blocks:
        used = {it.get("block") for _, it in items if it.get("block") is not None}
        skipped = {s.get("block") for s in markup.get("skipped", [])}
        loose = [b["id"] for b in blocks["blocks"] if b["id"] not in used and b["id"] not in skipped]
        if loose:
            problems.append(
                f"блоков не разобрано: {len(loose)} (id: {', '.join(map(str, loose[:20]))}"
                f"{'…' if len(loose) > 20 else ''})")

    for key in ("drawing", "designation", "title"):
        if not str(markup.get(key, "")).strip():
            problems.append(f"не заполнено поле «{key}»")

    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description="Проверка markup.json")
    ap.add_argument("markup", type=Path)
    ap.add_argument("--blocks", type=Path)
    args = ap.parse_args()

    markup = json.loads(args.markup.read_text(encoding="utf-8"))
    blocks = json.loads(args.blocks.read_text(encoding="utf-8")) if args.blocks else None

    problems = check(markup, blocks)
    total = sum(len(g.get("items", [])) for g in markup.get("groups", []))
    print(f"позиций: {total}, групп: {len(markup.get('groups', []))}")
    if not problems:
        print("замечаний нет")
        return
    print(f"замечаний: {len(problems)}")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)


if __name__ == "__main__":
    main()
