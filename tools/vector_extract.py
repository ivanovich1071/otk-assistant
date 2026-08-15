"""Выемка текста из векторного PDF без распознавания.

У чертежей из КОМПАС есть текстовый слой: надписи, штампы, технические
требования и целые листы спецификаций читаются точно, а не угадываются.
Для сканов остаётся растровый путь — `pdf_to_image.py` + `detect_text.py`.

Координаты блоков отдаются в пикселях выбранного dpi, как у `detect_text.py`,
чтобы дальше по конвейеру ничего не переписывать.

    python tools/vector_extract.py "input/деталь.pdf" -o work/vector.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from font_maps import decode_span, normalize, spec_text  # noqa: E402

MM = 72 / 25.4                 # пунктов в миллиметре
FRAME_WIDTH_MM = 185.0         # ширина основной надписи и таблицы спецификации
SPEC_ROW_GAP_PT = 8.0          # строки спецификации разделены больше чем на это

# Ячейки штампа — смещения центра надписи от якоря в миллиметрах:
# dx влево от правого края слова «Формат», dy вверх от его низа.
# Геометрия ГОСТ 2.104 одинакова на A4 и на A1, поэтому таблица одна на формат.
STAMP_FORM1 = {   # чертежи: основная надпись 185×55
    "designation": (10.0, 68.0, 44.0, 56.0),
    "title": (35.0, 92.0, 19.5, 43.0),
    "material": (35.0, 95.0, 4.0, 19.0),
    "mass": (-5.0, 9.0, 26.0, 37.0),
    "scale": (-24.0, -8.0, 26.0, 37.0),
    "sheet_format": (-13.0, -3.0, -1.0, 6.0),
}
STAMP_FORM2 = {   # листы спецификаций: основная надпись 185×40
    "designation": (10.0, 68.0, 33.0, 42.0),
    "title": (35.0, 92.0, 6.0, 30.0),
    "sheet_format": (-13.0, -3.0, -1.0, 6.0),
}
STAMP_HEIGHT_MM = {1: 55.0, 2: 40.0}

# Колонки спецификации по ГОСТ 2.106, границы в мм от левого края рамки.
SPEC_COLUMNS = [
    ("format", 0.0, 6.0),
    ("zone", 6.0, 12.0),
    ("pos", 12.0, 20.0),
    ("designation", 20.0, 90.0),
    ("name", 90.0, 153.0),
    ("qty", 153.0, 163.0),
    ("note", 163.0, 185.0),
]

SPEC_HEADER = {"Формат", "Зона", "Поз.", "Обозначение", "Наименование", "Кол."}
SPEC_HEADER_WORDS = SPEC_HEADER | {"Приме-", "чание"}
SPEC_SECTIONS = {
    "Документация", "Комплексы", "Сборочные единицы", "Детали",
    "Стандартные изделия", "Прочие изделия", "Материалы", "Комплекты",
}
DOC_TYPES = {
    "Сборочный чертеж", "Сборочный чертёж",
    "Габаритный чертеж", "Чертеж общего вида", "Чертёж общего вида",
}
# Надписи полей рамки и штампа — это не размеры, в поле чертежа их искать не надо.
FRAME_CAPTIONS = {
    "Инв. № подл.", "Подп. и дата", "Взам. инв. №", "Инв. № дубл.", "Справ. №",
    "Перв. примен.", "Копировал", "Формат", "Лист", "Листов", "Изм. Лист",
    "Изм.", "№ докум.", "Подп.", "Дата", "Разраб.", "Пров.", "Т.контр.",
    "Н.контр.", "Утв.", "Лит.", "Масса", "Масштаб", "Зона", "Поз.",
    "Обозначение", "Наименование", "Кол.", "Приме-", "чание", "Примечание",
}

_NUMBERED = re.compile(r"^\s*(\d{1,2})\.\s*")
_ANGLES = {(1.0, 0.0): 0, (0.0, -1.0): 90, (-1.0, 0.0): 180, (0.0, 1.0): 270}


def _angle(direction) -> int:
    key = (round(direction[0]), round(direction[1]))
    return _ANGLES.get((float(key[0]), float(key[1])), 0)


def read_lines(page) -> list[dict]:
    """Строки текстового слоя. PyMuPDF уже склеивает «Ø» из Symbol_A с числом."""
    out: list[dict] = []
    for block_no, block in enumerate(page.get_text("dict")["blocks"]):
        for line in block.get("lines", []):
            text = normalize("".join(
                decode_span(s["text"], s["font"]) for s in line["spans"]))
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            out.append({
                "text": text,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                "angle": _angle(line["dir"]),
                "size": round(max(s["size"] for s in line["spans"]), 1),
                "block": block_no,
            })
    return out


def read_words(page) -> list[dict]:
    """Отдельные слова с координатами — для граф спецификации.

    Строки не годятся: у стандартных изделий наименование и количество лежат
    в одной строке PDF, а по ГОСТ 2.106 это разные графы.
    """
    out: list[dict] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if _angle(line["dir"]) != 0:
                continue
            word, box = "", None
            for span in line["spans"]:
                for char in span["chars"]:
                    text = decode_span(char["c"], span["font"])
                    if text.isspace():
                        if word:
                            out.append({"text": word, "x0": box[0], "x1": box[2],
                                        "cy": (box[1] + box[3]) / 2})
                        word, box = "", None
                        continue
                    word += text
                    bbox = char["bbox"]
                    box = list(bbox) if box is None else [
                        min(box[0], bbox[0]), min(box[1], bbox[1]),
                        max(box[2], bbox[2]), max(box[3], bbox[3])]
            if word:
                out.append({"text": word, "x0": box[0], "x1": box[2],
                            "cy": (box[1] + box[3]) / 2})
    return out


def _long_verticals(page) -> list[float]:
    xs: list[float] = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l" and abs(item[1].x - item[2].x) < 0.6 \
                    and abs(item[1].y - item[2].y) > 60:
                xs.append(item[1].x)
            elif item[0] == "re" and item[1].width < 0.8 and item[1].height > 60:
                xs.append(item[1].x0)
    return xs


def frame_box(page) -> tuple[float, float]:
    """Левый и правый край рамки чертежа в пунктах.

    Берём пару вертикалей, расстояние между которыми ближе всего к 185 мм:
    крайняя правая линия — это обрез листа, а не рамка.
    """
    xs = sorted({round(x, 1) for x in _long_verticals(page)})
    target = FRAME_WIDTH_MM * MM
    best: tuple[float, float, float] | None = None
    for i, left in enumerate(xs):
        for right in xs[i + 1:]:
            err = abs((right - left) - target)
            if best is None or err < best[0]:
                best = (err, left, right)
    if best and best[0] < 3 * MM:
        return best[1], best[2]
    right = max(xs) if xs else page.rect.width - 5 * MM
    return right - target, right


def stamp_anchor(lines: list[dict], page) -> tuple[float, float] | None:
    """Якорь штампа — слово «Формат» в его нижней строке."""
    marks = [ln for ln in lines if ln["text"] == "Формат" and ln["angle"] == 0]
    if not marks:
        return None
    mark = max(marks, key=lambda ln: ln["y1"])
    if mark["y1"] < page.rect.height * 0.7:
        return None
    return mark["x1"], mark["y1"]


def _in_cell(line: dict, anchor: tuple[float, float], cell) -> bool:
    dx_min, dx_max, dy_min, dy_max = cell
    dx = (anchor[0] - line["cx"]) / MM
    dy = (anchor[1] - line["cy"]) / MM
    return dx_min <= dx <= dx_max and dy_min <= dy <= dy_max


def _cell_text(lines: list[dict], anchor, cell) -> list[dict]:
    """Строки ячейки сверху вниз, внутри строки — слева направо."""
    inside = [ln for ln in lines if _in_cell(ln, anchor, cell)]
    return sorted(inside, key=lambda ln: (round(ln["cy"], 1), ln["x0"]))


# Сортамент пишется дробью, а слово-приставка стоит слева от дробной черты
# и по высоте попадает сразу в обе строки: «Лист | 8 ГОСТ 19903-74 / Ст3сп ...».
SORTAMENT = {"Лист", "Круг", "Труба", "Полоса", "Уголок", "Швеллер", "Лента",
             "Проволока", "Пруток", "Квадрат", "Шестигранник"}


def sortament(texts: list[str]) -> str:
    """Строки материала сверху вниз — в одну строку записи.

    Слово сортамента стоит слева от дробной черты и по высоте попадает то в одну
    строку дроби, то в другую: «Лист | 8 ГОСТ 19903-74 / Ст3сп ГОСТ 14637-89».
    """
    prefix, rows = "", []
    for text in texts:
        head, _, tail = text.partition(" ")
        if head in SORTAMENT and not prefix:
            prefix = head
            if tail:
                rows.append(tail)
        else:
            rows.append(text)
    if not rows:
        return prefix
    if prefix:
        rows[0] = f"{prefix} {rows[0]}"
    return ", ".join(rows)


def read_stamp(lines: list[dict], page, form: int = 1) -> dict:
    anchor = stamp_anchor(lines, page)
    if anchor is None:
        return {}

    stamp: dict[str, str] = {}
    for field, cell in (STAMP_FORM1 if form == 1 else STAMP_FORM2).items():
        found = _cell_text(lines, anchor, cell)
        if field == "material":
            stamp[field] = sortament([ln["text"] for ln in found])
        elif field == "title":
            doc_type = [ln["text"] for ln in found if ln["text"] in DOC_TYPES]
            stamp["doc_type"] = doc_type[0] if doc_type else ""
            stamp[field] = " ".join(
                ln["text"] for ln in found if ln["text"] not in DOC_TYPES)
        else:
            stamp[field] = " ".join(ln["text"] for ln in found)
    return stamp


def tech_blocks(lines: list[dict]) -> set[int]:
    """Блоки с техническими требованиями.

    КОМПАС нередко разрывает список на два соседних блока — первый пункт
    оказывается отдельно. Берём блок с наибольшим числом нумерованных строк
    и всё, что стоит вплотную к нему тем же левым краем.
    """
    by_block: dict[int, list[dict]] = {}
    for line in lines:
        if line["angle"] == 0:
            by_block.setdefault(line["block"], []).append(line)

    scores = {block: sum(1 for ln in group if _NUMBERED.match(ln["text"]))
              for block, group in by_block.items()}
    anchor = max(scores, key=lambda b: scores[b], default=None)
    if anchor is None or scores[anchor] < 2:
        return set()

    box = by_block[anchor]
    left = min(ln["x0"] for ln in box)
    top, bottom = min(ln["y0"] for ln in box), max(ln["y1"] for ln in box)

    chosen = {anchor}
    for block, group in by_block.items():
        if block == anchor or not scores[block]:
            continue
        if abs(min(ln["x0"] for ln in group) - left) < 20 \
                and min(ln["y0"] for ln in group) > top - 60 \
                and max(ln["y1"] for ln in group) < bottom + 60:
            chosen.add(block)
    return chosen


def read_tech(lines: list[dict], blocks: set[int]) -> list[dict]:
    if not blocks:
        return []
    chosen = [ln for ln in lines if ln["block"] in blocks and ln["angle"] == 0]

    items: list[dict] = []
    for line in sorted(chosen, key=lambda ln: ln["y0"]):
        match = _NUMBERED.match(line["text"])
        if match:
            items.append({"no": match.group(1),
                          "text": line["text"][match.end():].strip()})
        elif items:
            items[-1]["text"] = f"{items[-1]['text']} {line['text'].strip()}".strip()
    return items


def is_spec(page, lines: list[dict]) -> bool:
    heads = {ln["text"] for ln in lines} & SPEC_HEADER
    return len(heads) >= 4


def _join(words: list[dict]) -> str:
    return spec_text(" ".join(w["text"] for w in words))


def _is_material(text: str) -> bool:
    """Продолжение строки с сортаментом или маркой — это материал, а не наименование."""
    return text in SORTAMENT or bool(re.match(r"^[\d,.]+\s+ГОСТ\s", text))


def read_spec(page, anchor) -> list[dict]:
    left, _ = frame_box(page)
    top_of_stamp = (anchor[1] - STAMP_HEIGHT_MM[2] * MM) if anchor else page.rect.height
    words = [w for w in read_words(page)
             if w["cy"] < top_of_stamp and w["text"] not in SPEC_HEADER_WORDS]

    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: w["cy"]):
        if rows and word["cy"] - max(w["cy"] for w in rows[-1]) <= SPEC_ROW_GAP_PT:
            rows[-1].append(word)
        else:
            rows.append([word])

    out: list[dict] = []
    section = ""
    for row in rows:
        cells: dict[str, list[dict]] = {name: [] for name, _, _ in SPEC_COLUMNS}
        for word in sorted(row, key=lambda w: w["x0"]):
            offset = (word["x0"] - left) / MM
            for name, start, end in SPEC_COLUMNS:
                if start <= offset < end:
                    cells[name].append(word)
                    break
        joined = {k: _join(v) for k, v in cells.items()}

        name = joined["name"]
        if name in SPEC_SECTIONS and not joined["designation"] and not joined["pos"]:
            section = name
            continue
        if not any(joined.values()):
            continue
        # Длинные наименование и примечание переносятся на следующую строку.
        # У детали без чертежа там же, в графе наименования, записан материал.
        if out and not joined["designation"] and not joined["pos"]:
            if name and (out[-1]["_material"] or _is_material(name)):
                out[-1]["_material"].append(name)
            elif name:
                out[-1]["name"] = f"{out[-1]['name']} {name}".strip()
            if joined["note"]:
                out[-1]["note"] = f"{out[-1]['note']} {joined['note']}".strip()
            if joined["qty"] and not out[-1]["qty"]:
                out[-1]["qty"] = joined["qty"]
            continue
        out.append({"section": section, "pos": joined["pos"],
                    "designation": joined["designation"], "name": name,
                    "qty": joined["qty"], "note": joined["note"], "_material": []})

    for row in out:
        row["material"] = sortament(row.pop("_material"))
    return out


def _zone(line: dict, anchor, tech_blocks: set[int], designation: str) -> str:
    if line["text"] in FRAME_CAPTIONS:
        return "frame"
    if line["block"] in tech_blocks:
        return "tt"
    if anchor is not None:
        dx = (anchor[0] - line["cx"]) / MM
        dy = (anchor[1] - line["cy"]) / MM
        if -30 <= dx <= FRAME_WIDTH_MM and -3 <= dy <= 58:
            return "stamp"
    # Обозначение повторяется в графе «Перв. примен.» в левом верхнем углу.
    if designation and line["text"] == designation:
        return "frame"
    return "field"


def extract_page(page, number: int, dpi: int) -> dict:
    lines = read_lines(page)
    scale = dpi / 72
    anchor = stamp_anchor(lines, page)
    spec_page = is_spec(page, lines)
    stamp = read_stamp(lines, page, form=2 if spec_page else 1)
    spec = read_spec(page, anchor) if spec_page else []
    tt_blocks = set() if spec_page else tech_blocks(lines)
    tech = read_tech(lines, tt_blocks)

    blocks = []
    for n, line in enumerate(lines, 1):
        blocks.append({
            "id": n,
            "x": int(line["x0"] * scale), "y": int(line["y0"] * scale),
            "w": int((line["x1"] - line["x0"]) * scale),
            "h": int((line["y1"] - line["y0"]) * scale),
            "angle": line["angle"],
            "text": line["text"],
            "size": line["size"],
            "zone": _zone(line, anchor, tt_blocks, stamp.get("designation", "")),
        })

    return {
        "page": number,
        "kind": "spec" if spec_page else ("drawing" if stamp else "unknown"),
        "width": int(page.rect.width * scale),
        "height": int(page.rect.height * scale),
        "stamp": stamp,
        "tech_requirements": tech,
        "spec": spec,
        "blocks": blocks,
    }


def extract(pdf: Path, dpi: int = 300) -> dict:
    doc = pymupdf.open(str(pdf))
    pages = [extract_page(page, n, dpi) for n, page in enumerate(doc, 1)]
    return {"pdf": str(pdf), "dpi": dpi, "pages": pages}


def has_text_layer(pdf: Path) -> bool:
    doc = pymupdf.open(str(pdf))
    return any(len(page.get_text("words")) > 20 for page in doc)


def main() -> None:
    ap = argparse.ArgumentParser(description="Текст из векторного PDF без распознавания")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("work/vector.json"))
    ap.add_argument("--dpi", type=int, default=300,
                    help="масштаб координат блоков, как у pdf_to_image.py")
    args = ap.parse_args()

    if not has_text_layer(args.pdf):
        raise SystemExit(
            "В PDF нет текстового слоя — это скан. Растровый путь: "
            "tools/pdf_to_image.py, затем tools/detect_text.py"
        )

    result = extract(args.pdf, args.dpi)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    kinds: dict[str, int] = {}
    for page in result["pages"]:
        kinds[page["kind"]] = kinds.get(page["kind"], 0) + 1
    fields = sum(1 for p in result["pages"] for b in p["blocks"] if b["zone"] == "field")
    spec_rows = sum(len(p["spec"]) for p in result["pages"])
    print(f"страниц: {len(result['pages'])} (" +
          ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")")
    print(f"надписей в поле чертежей: {fields}, строк спецификаций: {spec_rows}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
