"""Разметчик: контактные листы → `markup.json`.

Три шага, каждый со своей моделью и своей ценой:

1. **Чтение** — по контактному листу за запрос, дешёвая модель. Модель только
   читает надписи, ничего не решая.
2. **Разметка** — один текстовый запрос: что из прочитанного размер, к какому
   виду относится, какой номер получает. Картинок нет, поэтому дёшево.
3. **Перепроверка** — позиции с низкой уверенностью перечитываются по одной
   крупным планом сильной моделью. Ровно тот класс ошибок, ради которого
   в архитектуре есть Контролёр.

Координаты подставляет код: модель их не считает и не придумывает.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.llm import LLM, LLMError, parse_json

ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS = ROOT / "app" / "prompts"
RULES_DIR = ROOT / ".claude" / "rules"
MARKUP_RULES = ["numbering.md", "glossary.md", "tt-rules.md", "markup-format.md"]

RECHECK_SCALE = 5          # во сколько раз увеличить кроп при перепроверке
RECHECK_LIMIT = 12         # больше сомнительных — значит, дело не в отдельных надписях


def _prompt(name: str, rules: list[str] = ()) -> str:
    parts = [(PROMPTS / name).read_text(encoding="utf-8")]
    for rule in rules:
        path = RULES_DIR / rule
        if path.exists():
            parts.append(f"\n\n--- {rule} ---\n" + path.read_text(encoding="utf-8"))
    return "".join(parts)


def read_sheets(llm: LLM, sheets: list[dict], sheets_dir: Path,
                progress=None) -> dict[int, dict]:
    """Прочитанные надписи по номеру блока."""
    system = _prompt("reader.md", ["glossary.md"])
    texts: dict[int, dict] = {}

    for number, sheet in enumerate(sheets, 1):
        ids = sheet["ids"]
        user = (f"На листе {len(ids)} надписей с номерами: "
                f"{', '.join('#' + str(i) for i in ids)}.\n"
                "Верни JSON-массив по каждому номеру.")
        try:
            answer = parse_json(llm.ask(system, user, images=[sheets_dir / sheet["file"]]))
        except (LLMError, ValueError) as error:
            if progress:
                progress(f"лист {number}: {error}")
            continue

        for row in answer if isinstance(answer, list) else []:
            try:
                block = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            texts[block] = {"text": str(row.get("text", "")).strip(),
                            "sure": bool(row.get("sure", True))}
        if progress:
            progress(f"лист {number} из {len(sheets)}: прочитано {len(texts)}")
    return texts


def _blocks_digest(blocks: list[dict], texts: dict[int, dict]) -> list[dict]:
    out = []
    for block in blocks:
        read = texts.get(block["id"])
        if not read or not read["text"]:
            continue
        out.append({"id": block["id"], "text": read["text"],
                    "x": block["x"], "y": block["y"],
                    "w": block["w"], "h": block["h"], "angle": block["angle"],
                    "sure": read["sure"]})
    return out


def plan_markup(blocks: list[dict], texts: dict[int, dict],
                width: int, height: int, text_height: float,
                zones: dict | None = None) -> dict:
    """Группы и нумерация считаются кодом.

    Просить это у модели одним запросом пробовали: на 117 надписях она израсходовала
    24 000 токенов рассуждений и вернула пустой ответ ценой $0,035. Задача
    геометрическая, правила описаны в `numbering.md` однозначно — считает код.
    """
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import markup_layout                            # noqa: PLC0415

    return markup_layout.build(blocks, texts, width, height, text_height,
                               zones=zones)


def recheck(llm: LLM, markup: dict, image: Path, index: dict[int, dict],
            progress=None) -> int:
    """Сомнительные позиции — по одной, крупным планом, той же моделью.

    Второй взгляд помогает не тем, что модель «умнее», а тем, что надпись
    даётся отдельно и увеличенной в пять раз вместо мелкой ячейки листа.
    """
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import crop                                     # noqa: PLC0415

    doubtful = [(group, item) for group in markup["groups"] for item in group["items"]
                if item.get("confidence") == "low"]
    if not doubtful or len(doubtful) > RECHECK_LIMIT:
        return 0

    gray = crop.load_gray(image)
    system = _prompt("reader.md", ["glossary.md"])
    work = image.parent / "recheck"
    work.mkdir(parents=True, exist_ok=True)

    fixed = 0
    for _, item in doubtful:
        block = index.get(item["blocks"][0]) if item.get("blocks") else None
        if block is None:
            continue
        piece = crop.cut(gray, (block["x"], block["y"], block["w"], block["h"]),
                         block["angle"], scale=RECHECK_SCALE)
        path = work / f"item_{str(item['no']).replace('-', '_')}.png"
        crop.save(path, piece)
        try:
            answer = parse_json(llm.ask(
                system,
                f"На картинке одна надпись, её номер #{block['id']}. "
                f"Ранее её прочитали как «{item['value']}». Верни JSON-массив из одного объекта.",
                images=[path]))
        except (LLMError, ValueError):
            continue
        rows = answer if isinstance(answer, list) else []
        text = str(rows[0].get("text", "")).strip() if rows else ""
        if text and text != item["value"]:
            if progress:
                progress(f"позиция {item['no']}: «{item['value']}» → «{text}»")
            item["value"] = text
            fixed += 1
        item["confidence"] = "high" if text else "low"
    return fixed


def build_markup(plan: dict, blocks: list[dict], drawing: Path,
                 text_height: float) -> dict:
    """Координаты позиций берутся из blocks.json, а не из ответа модели."""
    index = {b["id"]: b for b in blocks}

    for group in plan.get("groups", []):
        kept = []
        for item in group.get("items", []):
            ids = [int(i) for i in item.get("blocks", []) if int(i) in index]
            if not ids:
                continue
            parts = [index[i] for i in ids]
            x = min(p["x"] for p in parts)
            y = min(p["y"] for p in parts)
            item.update({
                "kind": "text",
                "block": ids[0],
                "x": x, "y": y,
                "w": max(p["x"] + p["w"] for p in parts) - x,
                "h": max(p["y"] + p["h"] for p in parts) - y,
                "label_x": None, "label_y": None,
                "confidence": item.get("confidence", "high"),
            })
            kept.append(item)
        group["items"] = kept

    plan["groups"] = [g for g in plan.get("groups", []) if g["items"]]
    plan["drawing"] = str(drawing)
    plan["text_height"] = text_height
    plan.setdefault("tech_requirements", [])
    plan.setdefault("skipped", [])
    return plan


def run(image: Path, blocks_data: dict, sheets: list[dict], sheets_dir: Path,
        progress=None) -> tuple[dict, LLM]:
    llm = LLM()
    blocks = blocks_data["blocks"]
    index = {b["id"]: b for b in blocks}

    texts = read_sheets(llm, sheets, sheets_dir, progress)
    if not texts:
        raise LLMError("Ни одна надпись не прочитана")
    (image.parent / "texts.json").write_text(
        json.dumps(texts, ensure_ascii=False, indent=1), encoding="utf-8")

    zones = None
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import sheet_zones                          # noqa: PLC0415
        from detect_text import load_gray           # noqa: PLC0415
        zones = sheet_zones.analyze(load_gray(image))
        if progress:
            progress(f"служебных зон: таблиц {len(zones['tables'])}, "
                     f"проекций {len(zones['projections'])}")
    except Exception as error:                      # noqa: BLE001
        if progress:
            progress(f"зоны листа не определены ({error}) — отбор по краю листа")

    markup = plan_markup(blocks, texts, blocks_data["width"],
                         blocks_data["height"], blocks_data["text_height"], zones)
    markup["drawing"] = str(image)
    markup["text_height"] = blocks_data["text_height"]
    markup.setdefault("designation", "")
    markup.setdefault("title", "")
    recheck(llm, markup, image, index, progress)
    return markup, llm
