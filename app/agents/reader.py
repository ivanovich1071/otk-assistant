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
MARKUP_RULES = ["sheet-zones.md", "numbering.md", "glossary.md", "tt-rules.md",
                "markup-format.md"]

RECHECK_SCALE = 5          # во сколько раз увеличить кроп при перепроверке
RECHECK_LIMIT = 12         # больше сомнительных — значит, дело не в отдельных надписях
STAMP_SCALE = 2            # штамп крупный сам по себе, хватает двукратного

# Типы документа из графы 1, при которых лист — сборка, а не деталь.
ASSEMBLY_TYPES = ("сборочный", "габаритный", "монтажный")


def is_assembly(stamp: dict, hint: str = "", zones: dict | None = None) -> bool:
    """Сборочный чертёж — по любому из четырёх признаков.

    На сборке размеры нумеруются, а номера позиций деталей — нет, поэтому
    признак нужен до разметки, и ошибиться в нём дорого: на «Колесе тяговом»
    модель вернула пустое обозначение (кроп штампа был срезан кривой рамкой),
    и четыре номера позиций ушли в карту размерами. Одного источника мало.
    """
    kind = (stamp.get("doc_type") or "").strip().lower()
    if any(word in kind for word in ASSEMBLY_TYPES):
        return True
    for name in ((stamp.get("designation") or ""), hint):
        if name.strip().upper().replace(" ", "").endswith("СБ"):
            return True
    # Спецификация по ГОСТ 2.106 стоит над штампом только на сборочном чертеже.
    return bool((zones or {}).get("spec"))


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


STAMP_FIELDS = ("designation", "title", "doc_type", "material", "mass",
                "scale", "sheet", "sheets")


def read_stamp_image(llm: LLM, path: Path, progress=None) -> dict:
    """Графы основной надписи по готовому кропу штампа.

    Отдельно от `read_stamp`, потому что оглавление комплекта берёт штамп
    вырезкой прямо из PDF по ГОСТ 2.104 и целый лист не рендерит.
    """
    try:
        answer = parse_json(llm.ask(_prompt("stamp.md"),
                                    "Верни JSON-объект с графами штампа.",
                                    images=[path]))
    except (LLMError, ValueError) as error:
        if progress:
            progress(f"штамп не прочитан: {error}")
        return {}
    if not isinstance(answer, dict):
        return {}

    stamp = {key: str(answer.get(key, "")).strip() for key in STAMP_FIELDS}
    if progress:
        progress(f"штамп: {stamp['designation']} {stamp['title']}".strip())
    return stamp


def read_stamp(llm: LLM, image: Path, zones: dict, progress=None) -> dict:
    """Основная надпись одним запросом по кропу.

    Зона штампа отсчитана от рамки чертежа, поэтому кроп попадает в неё и на
    сканах с произвольными полями. Раньше шапка карты оставалась пустой,
    а файл назывался по имени исходника.
    """
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import crop                                     # noqa: PLC0415

    x0, y0, x1, y1 = zones["stamp"]
    piece = crop.cut(crop.load_gray(image), (x0, y0, x1 - x0, y1 - y0),
                     pad=0, scale=STAMP_SCALE)
    path = image.parent / "stamp.png"
    crop.save(path, piece)
    return read_stamp_image(llm, path, progress)


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
                zones: dict | None = None, assembly: bool = False,
                pitch: float | None = None) -> dict:
    """Группы и нумерация считаются кодом.

    Просить это у модели одним запросом пробовали: на 117 надписях она израсходовала
    24 000 токенов рассуждений и вернула пустой ответ ценой $0,035. Задача
    геометрическая, правила описаны в `numbering.md` однозначно — считает код.
    """
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import markup_layout                            # noqa: PLC0415

    return markup_layout.build(blocks, texts, width, height, text_height,
                               zones=zones, assembly=assembly, pitch=pitch)


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
        progress=None, hint: str = "") -> tuple[dict, LLM]:
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
        import markup_layout                        # noqa: PLC0415
        pitch = markup_layout.line_height(blocks, blocks_data["text_height"],
                                          blocks_data.get("pitch"))
        zones = sheet_zones.analyze(load_gray(image), blocks, pitch)
        if progress:
            progress(f"служебных зон: таблиц {len(zones['tables'])}, "
                     f"проекций {len(zones['projections'])}"
                     + (", спецификация найдена" if zones.get("spec") else ""))
    except Exception as error:                      # noqa: BLE001
        if progress:
            progress(f"зоны листа не определены ({error}) — отбор по краю листа")

    stamp = read_stamp(llm, image, zones, progress) if zones else {}
    assembly = is_assembly(stamp, hint, zones)
    if assembly and progress:
        progress("сборочный чертёж — номера позиций деталей не нумеруются")

    markup = plan_markup(blocks, texts, blocks_data["width"],
                         blocks_data["height"], blocks_data["text_height"],
                         zones, assembly, blocks_data.get("pitch"))
    markup["drawing"] = str(image)
    markup["text_height"] = blocks_data["text_height"]
    markup["pitch"] = blocks_data.get("pitch")
    markup["stamp"] = stamp
    markup["assembly"] = assembly
    markup["designation"] = stamp.get("designation", "")
    markup["title"] = stamp.get("title", "")
    recheck(llm, markup, image, index, progress)
    return markup, llm
