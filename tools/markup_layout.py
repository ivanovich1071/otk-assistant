"""Разбиение прочитанных надписей на группы и нумерация — без модели.

Модель однажды уже попробовали просить сделать это одним запросом: она потратила
24 000 токенов на рассуждения и вернула пустой ответ. Задача геометрическая,
и правила её описаны в `numbering.md` однозначно, поэтому считает код.

Модель остаётся тем, чем была: глазами. Она читает надписи, а не решает,
где кончается один вид и начинается другой.
"""
from __future__ import annotations

import re
import statistics

DPI = 300
MM = DPI / 25.4                     # пикселей в миллиметре

STAMP_W_MM, STAMP_H_MM = 185.0, 60.0
MARGIN_MM = 5.0

# Заголовки проекций: буква без масштаба — вид, с масштабом — выноска,
# «А-А» — разрез (см. numbering.md).
VIEW_LETTER = re.compile(r"^[А-ЯA-Z]$")
# Буква разреза бывает с индексом: «А₁—А₁». Индекс детектор видит цифрой.
SECTION = re.compile(r"^([А-ЯA-Z]\d?)\s*[-–—]\s*\1$")
SECTION_MARK = re.compile(r"^[А-ЯA-Z]\d?$")
CALLOUT = re.compile(r"^([А-ЯA-Z])\s*\(\s*\d+\s*:\s*[\d,]+\s*\)$")
# Детектор часто режет номер пункта в отдельный блок, поэтому после точки
# может не быть ничего: «1.» — это тоже начало технического требования.
NUMBERED_TT = re.compile(r"^\s*\d{1,2}\.(\s|$)")
# В собранной строке номер пункта бывает и без точки — «1 *Размеры исполнительные»,
# и с подпунктом — «5.1 зафиксировать шпильки».
TT_ITEM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)\s*[.)]?\s+(?=\S)")
HAS_DIGIT = re.compile(r"\d")

# Таблица параметров зубчатого венца по ГОСТ 2.403 (и её родня — ГОСТ 2.404,
# 2.406 для звёздочек и червяков). Её содержимое — предмет обмера, а не служебная
# графа: модуль и число зубьев в карте нужны так же, как диаметры.
PARAMETER_WORDS = (
    "модуль", "число зубьев", "делительный диаметр", "исходный контур",
    "коэффициент смещения", "степень точности", "угол наклона", "направление зуба",
    "нормальный модуль", "число зубьев звёздочки", "шаг цепи", "профиль",
    "длина общей нормали", "делительная окружность", "толщина зуба",
)

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

# Текстовая часть листа: несколько слов подряд в строку и несколько таких строк
# друг под другом с одного отступа. Подобрано на «Станине КВ2536-11-001 СБ».
TEXT_WORDS = 4                  # столько слов подряд — уже строка текста
TEXT_LINES = 3                  # столько строк друг под другом — абзац
TEXT_SPACE = 1.6                # пробел между словами строки, в кеглях
TEXT_PITCH = 2.5                # разрыв между строками абзаца, в кеглях
TEXT_INDENT = 2.5               # разброс левого края строк абзаца, в кеглях
TEXT_RUN_WIDTH = 4.0            # строка уже этого — обрывок, а не текст
TEXT_WIDE = 15.0                # такую длину размер не набирает — это фраза

# Номера позиций деталей на сборочном чертеже. По ГОСТ 2.109 номер стоит на
# полке-выноске, по ГОСТ 2.316 полки группируют в колонку или строчку.
# Пороги сверены на «Станине КВ2536-11-001 СБ»: 22 номера показаны модели
# крупным планом, её вердикт совпал с геометрией в 21 случае. До 3,65 шли
# сплошь позиции, от 3,8 — размеры.
POSITION_NUMBER = re.compile(r"^\d{1,3}$")
LEADER_SHELF = 3.7              # линия под номером короче — полка выноски
LEADER_DOUBT = 5.0              # в полосе до этого признак ненадёжен


def stamp_zone(width: int, height: int) -> tuple[float, float]:
    """Левая и верхняя границы основной надписи в пикселях."""
    return (width - (MARGIN_MM + STAMP_W_MM) * MM,
            height - (MARGIN_MM + STAMP_H_MM) * MM)


def line_height(blocks: list[dict], text_height: float) -> float:
    """Кегль основного текста листа.

    Оценка детектора занижена — в неё входят обломки штриховки и засечки:
    на «Станине» она дала 36 px при реальной высоте цифры около 68. Медиана
    по блокам крупнее этой оценки и есть кегль.
    """
    tall = [b["h"] for b in blocks if b["h"] >= text_height]
    return float(statistics.median(tall)) if tall else float(text_height)


def _text_runs(blocks: list[dict], pitch: float) -> list[list[dict]]:
    """Связные отрезки строк: слова, стоящие подряд вплотную.

    Одной привязки по вертикали мало: на листе шириной девять тысяч пикселей
    в любую полосу попадают размеры с разных концов чертежа. Строкой считается
    только то, что идёт подряд с пробелами в один кегль.
    """
    rows: list[tuple[float, list[dict]]] = []
    for block in sorted((b for b in blocks if b["angle"] == 0),
                        key=lambda b: b["y"] + b["h"] / 2):
        centre = block["y"] + block["h"] / 2
        if rows and centre - rows[-1][0] < pitch * 0.5:
            rows[-1][1].append(block)
        else:
            rows.append((centre, [block]))

    runs: list[list[dict]] = []
    for _, row in rows:
        run: list[dict] = []
        for block in sorted(row, key=lambda b: b["x"]):
            if run and block["x"] - (run[-1]["x"] + run[-1]["w"]) > pitch * TEXT_SPACE:
                runs.append(run)
                run = []
            run.append(block)
        if run:
            runs.append(run)

    # Слов в строке бывает и меньше четырёх: модель иногда возвращает половину
    # фразы одним блоком. Тогда строку выдаёт длина — размер её не набирает.
    return [r for r in runs
            if (r[-1]["x"] + r[-1]["w"] - r[0]["x"] >= pitch * TEXT_WIDE
                or (len(r) >= TEXT_WORDS
                    and r[-1]["x"] + r[-1]["w"] - r[0]["x"] >= pitch * TEXT_RUN_WIDTH))]


def _run_box(run: list[dict]) -> tuple[int, int, int, int]:
    return (min(b["x"] for b in run), min(b["y"] for b in run),
            max(b["x"] + b["w"] for b in run), max(b["y"] + b["h"] for b in run))


def _service(box: tuple[int, int, int, int], zones: dict | None) -> bool:
    """Абзац сидит в штампе или в графе обозначения — это не текст чертежа."""
    if not zones:
        return False
    import sheet_zones                              # noqa: PLC0415
    rect = (box[0], box[1], box[2] - box[0], box[3] - box[1])
    return any(sheet_zones.inside(rect, zones[name], part=0.5)
               for name in ("stamp", "extra") if zones.get(name))


def text_columns(blocks: list[dict], text_height: float,
                 zones: dict | None = None) -> list[list[list[dict]]]:
    """Абзацы сплошного текста: технические требования и прочие надписи.

    Текст отличается от размеров не содержанием, а видом. Раньше блок ТТ искали
    по строкам с номером пункта — на скане детектор рвёт строку на слова, якорь
    не находился, и весь текст требований уходил в карту размерами. Геометрия
    от чтения не зависит.
    """
    pitch = line_height(blocks, text_height)
    columns: list[list[list[dict]]] = []
    for run in sorted(_text_runs(blocks, pitch), key=lambda r: _run_box(r)[1]):
        left, top, _, _ = _run_box(run)
        for column in columns:
            plast, _, _, pbottom = _run_box(column[-1])
            if abs(left - plast) < pitch * TEXT_INDENT \
                    and -pitch * 0.5 <= top - pbottom < pitch * TEXT_PITCH:
                column.append(run)
                break
        else:
            columns.append([run])
    return [c for c in columns
            if len(c) >= TEXT_LINES
            and not _service(_run_box([b for run in c for b in run]), zones)]


def text_zones(blocks: list[dict], text_height: float,
               zones: dict | None = None) -> list[tuple[int, int, int, int]]:
    pitch = line_height(blocks, text_height)
    pad = int(pitch * 0.4)
    out = []
    for column in text_columns(blocks, text_height, zones):
        flat = [b for run in column for b in run]
        x0, y0, x1, y1 = _run_box(flat)
        out.append((x0 - pad, y0 - pad, x1 + pad, y1 + pad))
    return out


def classify(block: dict, text: str, width: int, height: int,
             zones: dict | None = None) -> str:
    """Что это за надпись: размер, заголовок, штамп, техтребование или мусор."""
    value = (text or "").strip()
    if not value:
        return "junk"
    # Буква с индексом — метка разреза «Б₁» или его заголовок. По numbering.md
    # буквенные обозначения видов, разрезов и поверхностей не нумеруются.
    # Хвост от стрелки взгляда модель дописывает к метке: «Б₁» приходит как
    # «D1—» или «D1\», поэтому кайма из штрихов снимается перед проверкой.
    core = value.strip(" .,;:—–-\\/|_'\"")
    if VIEW_LETTER.match(core) or SECTION.match(core) or CALLOUT.match(core) \
            or SECTION_MARK.match(core):
        return "caption"

    box = (block["x"], block["y"], block["w"], block["h"])
    if zones:
        # Зоны найдены по линиям листа: штамп отсчитан от рамки чертежа, а не от
        # края листа, поэтому на сканах с произвольными полями не промахивается.
        import sheet_zones                          # noqa: PLC0415
        if sheet_zones.inside(box, zones["stamp"]):
            return "stamp"
        if zones.get("extra") and sheet_zones.inside(box, zones["extra"]):
            return "extra"
        if any(sheet_zones.inside(box, table) for table in zones["tables"]):
            return "table"
        if not sheet_zones.inside(box, zones["frame"], part=0.5):
            return "frame"
    else:
        stamp_x, stamp_y = stamp_zone(width, height)
        if block["x"] + block["w"] > stamp_x and block["y"] + block["h"] > stamp_y:
            return "stamp"
        if block["x"] < 15 * MM or block["y"] < 8 * MM:
            return "frame"                  # колонки «Инв. № подл.» и верхний угол
    if NUMBERED_TT.match(value):
        return "tt"
    words = re.findall(r"[А-Яа-яA-Za-z]+", value.lower())
    if words and all(w[:6] in FRAME_WORDS or w in FRAME_WORDS for w in words):
        return "frame"
    if not HAS_DIGIT.search(value):
        return "text"
    return "size"


def leader(zones: dict | None, block_id: int) -> float | None:
    """Длина линии под надписью в её ширинах — считает `sheet_zones`."""
    table = (zones or {}).get("leader") or {}
    if block_id in table:
        return table[block_id]
    return table.get(str(block_id))


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


def _distance(entry: dict, box: list[int]) -> float:
    dx = max(box[0] - entry["cx"], 0.0, entry["cx"] - box[2])
    dy = max(box[1] - entry["cy"], 0.0, entry["cy"] - box[3])
    return (dx * dx + dy * dy) ** 0.5


def by_projection(items: list[dict], projections: list[list[int]]) -> list[list[dict]]:
    """Размер относится к той проекции, рядом с которой стоит.

    Резать лист по коридорам пустоты не выходило: у одной проекции размеры
    стоят ярусами далеко друг от друга, у соседней — вплотную, и главный вид
    разваливался, слипаясь с текстом рядом. Проекции же `sheet_zones` находит
    по плотным сгусткам линий — это и есть виды, разрезы и выноски.
    """
    groups: list[list[dict]] = [[] for _ in projections]
    for entry in items:
        nearest = min(range(len(projections)),
                      key=lambda i: _distance(entry, projections[i]))
        groups[nearest].append(entry)
    return [g for g in groups if g]


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

    # Заголовок принадлежит одной проекции. Без этого соседние группы получали
    # общую ближайшую букву и в карте оказывалось два «Вида M».
    claimed: dict[int, int] = {}
    for index, (cx, cy) in enumerate(centres):
        found = _nearest_caption(cx, cy, captions)
        if found is None:
            continue
        rival = claimed.get(found)
        if rival is None or cy - captions[found]["cy"] < centres[rival][1] - captions[found]["cy"]:
            claimed[found] = index

    owner = {group: caption for caption, group in claimed.items()}

    names = []
    for index, (cx, cy) in enumerate(centres):
        caption = _caption_text(captions[owner[index]]) if index in owner else ""
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

    # Проекций с одной стороны от главного вида бывает несколько, а буквенных
    # заголовков на скане может не найтись. Одинаковые имена нумеруем, иначе
    # в карте две группы «Вид слева» неразличимы.
    total = {name: names.count(name) for name in set(names)}
    seen: dict[str, int] = {}
    for index, name in enumerate(names):
        if total[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            names[index] = f"{name} {seen[name]}"
    return names


def section_titles(blocks: list[dict], texts: dict[int, dict],
                   pitch: float) -> list[dict]:
    """Заголовки разрезов «А₁—А₁»: детектор режет их на два блока.

    Тире между буквами теряется вместе с индексом, и каждая половина выглядит
    обычной короткой надписью. Пара одинаковых меток рядом в строке — заголовок.
    """
    rows: list[list[dict]] = []
    for block in sorted((b for b in blocks if b["angle"] == 0),
                        key=lambda b: b["y"] + b["h"] / 2):
        centre = block["y"] + block["h"] / 2
        if rows and centre - (rows[-1][0]["y"] + rows[-1][0]["h"] / 2) < pitch * 0.6:
            rows[-1].append(block)
        else:
            rows.append([block])

    out = []
    for row in rows:
        line = sorted(row, key=lambda b: b["x"])
        for first, second in zip(line, line[1:]):
            left = (texts.get(first["id"]) or {}).get("text", "").strip()
            right = (texts.get(second["id"]) or {}).get("text", "").strip()
            if not left or left != right or not SECTION_MARK.match(left):
                continue
            if second["x"] - (first["x"] + first["w"]) > pitch * 4:
                continue
            out.append({
                "id": first["id"], "text": f"{left}-{right}", "value": f"{left}-{right}",
                "cx": (first["x"] + second["x"] + second["w"]) / 2,
                "cy": first["y"] + first["h"] / 2,
                "x": first["x"], "y": first["y"],
                "w": second["x"] + second["w"] - first["x"], "h": first["h"],
                "sure": True,
            })
    return out


def _nearest_caption(cx: float, cy: float, captions: list[dict]) -> int | None:
    """Индекс ближайшего заголовка над проекцией."""
    near = [i for i, c in enumerate(captions)
            if abs(c["cx"] - cx) < 900 and c["cy"] < cy]
    if not near:
        return None
    return min(near, key=lambda i: cy - captions[i]["cy"])


def _caption_text(caption: dict) -> str:
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


def parameter_tables(blocks: list[dict], texts: dict[int, dict],
                     zones: dict | None) -> list[list[int]]:
    """Таблицы параметров среди прочих таблиц листа.

    Штамп, спецификация и таблица изменений в карту не идут, а параметры венца
    идут: модуль, число зубьев и делительный диаметр обмеряются наравне
    с диаметрами. Отличаем по содержимому — по словам ГОСТ 2.403.
    """
    tables = (zones or {}).get("tables") or []
    if not tables:
        return []
    import sheet_zones                              # noqa: PLC0415

    out = []
    for table in tables:
        words = " ".join(
            (texts.get(b["id"]) or {}).get("text", "").lower()
            for b in blocks
            if sheet_zones.inside((b["x"], b["y"], b["w"], b["h"]), table))
        if any(word in words for word in PARAMETER_WORDS):
            out.append(table)
    return out


def table_rows(blocks: list[dict], texts: dict[int, dict],
               table: list[int], pitch: float) -> list[dict]:
    """Строки таблицы: слева наименование параметра, справа значение."""
    import sheet_zones                              # noqa: PLC0415

    inside = [b for b in blocks
              if sheet_zones.inside((b["x"], b["y"], b["w"], b["h"]), table)]
    rows: list[list[dict]] = []
    for block in sorted(inside, key=lambda b: b["y"] + b["h"] / 2):
        centre = block["y"] + block["h"] / 2
        if rows and centre - (rows[-1][0]["y"] + rows[-1][0]["h"] / 2) < pitch * 0.6:
            rows[-1].append(block)
        else:
            rows.append([block])

    out = []
    for row in rows:
        words = [(texts.get(b["id"]) or {}).get("text", "").strip()
                 for b in sorted(row, key=lambda b: b["x"])]
        line = " ".join(w for w in words if w)
        if line:
            out.append({"no": "", "text": line})
    return out


def tech_items(columns: list[list[list[dict]]],
               texts: dict[int, dict]) -> list[dict]:
    """Технические требования строками, а не обломками слов.

    Раньше в секцию попадал каждый блок отдельно, и вместо пункта выходило
    `1.1)/(` и `67 /`. Строка собирается из своего отрезка слов, продолжение
    без номера приклеивается к предыдущему пункту.
    """
    items: list[dict] = []
    for column in columns:
        for run in sorted(column, key=lambda r: _run_box(r)[1]):
            words = [(texts.get(b["id"]) or {}).get("text", "").strip() for b in run]
            line = " ".join(w for w in words if w)
            if not line:
                continue
            match = TT_ITEM.match(line)
            if match:
                items.append({"no": match.group(1), "text": line[match.end():].strip()})
            elif items:
                items[-1]["text"] = f"{items[-1]['text']} {line}".strip()
            else:
                items.append({"no": "", "text": line})
    return items


def build(blocks: list[dict], texts: dict[int, dict], width: int, height: int,
          text_height: float, zones: dict | None = None,
          assembly: bool = False) -> dict:
    """Готовые группы с номерами и координатами."""
    sizes, captions, tech, skipped = [], [], [], []
    doubtful: set[int] = set()
    columns = text_columns(blocks, text_height, zones)
    boxes = text_zones(blocks, text_height, zones)

    for block in blocks:
        read = texts.get(block["id"])
        value = (read or {}).get("text", "").strip()
        kind = classify(block, value, width, height, zones)
        # Текстовая часть листа отсекается до разбора значения: под номер размера
        # маскируются и «ГОСТ 30893.1-2002», и «0,04мм», и «поз. 94».
        in_text = any(_in_box(block, b) for b in boxes)
        if kind in ("size", "text", "caption") and in_text:
            kind = "tt"
        if assembly and kind == "size" and POSITION_NUMBER.match(value):
            ratio = leader(zones, block["id"])
            if ratio is not None and ratio < LEADER_SHELF:
                kind = "position"
            elif ratio is not None and ratio < LEADER_DOUBT:
                # Пограничная полка. Размер остаётся в карте, но жёлтым:
                # снять лишнюю строку дешевле, чем найти потерянную.
                doubtful.add(block["id"])
        entry = {"id": block["id"], "value": value,
                 "cx": block["x"] + block["w"] / 2, "cy": block["y"] + block["h"] / 2,
                 "x": block["x"], "y": block["y"], "w": block["w"], "h": block["h"],
                 "text": value, "sure": (read or {}).get("sure", True)}
        if kind == "size":
            sizes.append(entry)
        elif kind == "caption":
            captions.append(entry)
            skipped.append({"block": block["id"], "reason": kind})
        elif kind == "tt":
            # Текст абзаца собирается построчно в tech_items; сюда идут только
            # одиночные пункты, не попавшие ни в один абзац.
            if not in_text:
                tech.append(entry)
            skipped.append({"block": block["id"], "reason": kind})
        else:
            # Пустые блоки тоже записываем: иначе проверка считает их
            # неразобранными и на скане выдаёт «блоков не разобрано: 943».
            skipped.append({"block": block["id"], "reason": kind})

    projections = (zones or {}).get("projections") or []
    groups = by_projection(sizes, projections) if projections \
        else cluster(sizes, text_height)
    # Заголовок вида стоит над проекцией, а буква обозначения поверхности —
    # на самой детали. Иначе на «Станине» две группы получали общее «Вид M».
    if projections:
        captions = [c for c in captions
                    if not any(_distance(c, p) == 0 for p in projections)]
    captions += section_titles(blocks, texts, line_height(blocks, text_height))
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
                "confidence": ("high" if entry["sure"]
                                        and entry["id"] not in doubtful else "low"),
            })
            number += 1
        result.append({"name": names[index], "items": items})

    items = tech_items(columns, texts)
    items += [{"no": "", "text": e["value"]}
              for e in sorted(tech, key=lambda e: e["cy"]) if e["value"]]

    pitch = line_height(blocks, text_height)
    parameters: list[dict] = []
    for table in parameter_tables(blocks, texts, zones):
        parameters += table_rows(blocks, texts, table, pitch)

    return {
        "groups": result,
        "tech_requirements": items,
        "parameters": parameters,
        "skipped": skipped,
    }
