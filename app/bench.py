"""Бенчмарк чтения чертёжных надписей vision-моделью.

Точка «идём/не идём» перед постройкой приложения: проверяем, читает ли модель
шрифт ГОСТ и спецсимволы. Эталон — значения из готовой карты обмера.

    python -m app.bench --gold tests/gold/5489.0123.0000.28 --env "путь к .env"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import crop  # noqa: E402
import detect_text  # noqa: E402

from app.llm import LLM, MODEL_MAIN, parse_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

SYSTEM = """Ты читаешь надписи с машиностроительного чертежа, выполненного шрифтом
по ГОСТ 2.304 (наклонный, цифры и буквы с характерными засечками).

Тебе дают контактный лист: несколько вырезанных надписей, каждая подписана
красным номером вида #12. Повёрнутые надписи уже развёрнуты в горизонтальное положение.

Верни СТРОГО JSON-массив, без пояснений:
[{"id": 12, "text": "Ø329", "type": "размер"}, ...]

Правила записи text:
- переписывай ровно то, что видишь, ничего не исправляя и не додумывая
- диаметр — символ Ø (не Ф, не 0, не фи)
- градус — °, угловая минута — ´, дюйм — две ´´
- знак умножения в фаске — ×
- десятичный разделитель — запятая, как на чертеже
- поля допуска латиницей: H8, h7, k6, H14
- звёздочки справочных размеров сохраняй: 863*, R0,2**
- если в ячейке не текст, а обломок графики (штриховка, дуга, стрелка, кусок линии) —
  text оставь пустым

Значения type: размер, заголовок вида, буква, штамп, техтребования, мусор."""

USER = """Прочитай все надписи с этого контактного листа.
На листе {n} ячеек с номерами: {ids}.
Верни JSON-массив ровно с этими id, ни одного не пропусти."""


def normalize(s: str) -> str:
    """Сравниваем содержание, а не вёрстку: пробелы и регистр не важны."""
    s = s.replace(" ", " ").strip().lower()
    s = re.sub(r"\s+", "", s)
    return s


def gold_values(card: Path) -> list[str]:
    import docx

    doc = docx.Document(str(card))
    out: list[str] = []
    for row in doc.tables[0].rows:
        cells = [c.text.strip() for c in row.cells]
        if len(cells) > 1 and cells[0] and cells[1]:
            out.append(cells[1])
    return out


def prepare(drawing: Path, work: Path) -> tuple[dict, list[dict]]:
    work.mkdir(parents=True, exist_ok=True)
    blocks_file = work / "blocks.json"
    if not blocks_file.exists():
        result = detect_text.detect(drawing)
        blocks_file.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    blocks = json.loads(blocks_file.read_text(encoding="utf-8"))

    sheets_dir = work / "sheets"
    index = sheets_dir / "index.json"
    if not index.exists():
        gray = crop.load_gray(drawing)
        sheets = crop.make_sheets(gray, blocks["blocks"], sheets_dir, 24, 3)
        index.write_text(json.dumps({"sheets": sheets}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return blocks, json.loads(index.read_text(encoding="utf-8"))["sheets"]


def read_sheets(llm: LLM, sheets_dir: Path, sheets: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for i, sheet in enumerate(sheets, 1):
        ids = sheet["ids"]
        answer = llm.ask(
            SYSTEM,
            USER.format(n=len(ids), ids=", ".join(f"#{i}" for i in ids)),
            images=[sheets_dir / sheet["file"]],
        )
        try:
            items = parse_json(answer)
        except Exception as e:
            print(f"  лист {i}: не разобрал ответ ({e})")
            continue
        got = 0
        for item in items:
            try:
                out[int(item["id"])] = {"text": str(item.get("text", "")).strip(),
                                        "type": str(item.get("type", ""))}
                got += 1
            except (KeyError, TypeError, ValueError):
                continue
        print(f"  лист {i}/{len(sheets)}: запрошено {len(ids)}, получено {got}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Бенчмарк чтения надписей моделью")
    ap.add_argument("--gold", type=Path, required=True,
                    help="папка эталона: drawing.jpg + card.docx")
    ap.add_argument("--model", default=MODEL_MAIN)
    ap.add_argument("--env", type=Path, help="файл .env с ключом OpenRouter")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    gold = args.gold if args.gold.is_absolute() else ROOT / args.gold
    drawing, card = gold / "drawing.jpg", gold / "card.docx"
    for f in (drawing, card):
        if not f.exists():
            raise SystemExit(f"нет файла: {f}")

    work = ROOT / "work" / "bench" / gold.name
    print(f"Чертёж: {drawing.name}")
    blocks, sheets = prepare(drawing, work)
    print(f"надписей: {len(blocks['blocks'])}, контактных листов: {len(sheets)}")

    llm = LLM(model=args.model, env_file=args.env, use_cache=not args.no_cache)
    print(f"модель: {args.model}\n")
    read = read_sheets(llm, work / "sheets", sheets)

    (work / "read.json").write_text(
        json.dumps(read, ensure_ascii=False, indent=1), encoding="utf-8")

    expected = gold_values(card)
    pool = {normalize(v["text"]) for v in read.values() if v["text"]}
    # Составное значение карты («R8 4 радиуса») собирается из нескольких надписей —
    # засчитываем, если каждая часть нашлась среди прочитанного.
    found, missing = [], []
    for value in expected:
        n = normalize(value)
        if n in pool or any(n.startswith(p) and len(p) > 2 for p in pool):
            found.append(value)
        else:
            missing.append(value)

    total = len(expected)
    print(f"\n{'=' * 60}")
    print(f"Эталонных значений: {total}")
    print(f"Найдено среди прочитанного: {len(found)} ({100 * len(found) / max(1, total):.0f}%)")
    print(f"Прочитано надписей всего: {sum(1 for v in read.values() if v['text'])}"
          f" из {len(blocks['blocks'])} блоков")
    print(llm.usage.summary())
    if missing:
        print(f"\nНе нашлись ({len(missing)}):")
        for value in missing:
            print(f"  · {value[:80]}")
    print(f"\nПодробности: {work / 'read.json'}")


if __name__ == "__main__":
    main()
