"""Оглавление многостраничного комплекта.

По каждому листу — формат, тип (чертёж или спецификация), обозначение,
наименование, «Лист N из M» и оценка времени и денег на карту обмера.
Дальше приложение показывает это оглавление и заводит задания по отмеченным
листам.

Дорогих действий два, и оба здесь избегаются:

- **полный рендер листа в 300 dpi** — A0 это 16 секунд и 137 мегапикселей.
  Тип листа проверяется рендером только у A4, потому что спецификацию
  по ГОСТ 2.106 выполняют на A4 и ни на чём другом;
- **чтение штампа моделью** — берётся кроп 185×55 мм по ГОСТ 2.104, а не
  весь лист, и только у листов-чертежей.

    python tools/sheet_index.py "комплект.pdf" -o work/sheets.json [--no-stamps]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sheet_zones                                  # noqa: E402
from detect_text import (binarize, estimate_text_height,      # noqa: E402
                         load_gray, remove_long_lines)
from vector_extract import SPEC_HEADER              # noqa: E402

MM = 72 / 25.4                  # пунктов в миллиметре
DPI = 300                       # в этом разрешении работает весь конвейер
PX_MM = DPI / 25.4              # пикселей в миллиметре на рендере листа

# Форматы по ГОСТ 2.301. Сравниваем по площади: скан режут неровно, и лист
# «A3» приходит то 295×418, то 302×424.
FORMATS = [("A4", 210, 297), ("A3", 297, 420), ("A2", 420, 594),
           ("A1", 594, 841), ("A0", 841, 1189)]
FORMAT_TOLERANCE = 1.3          # во столько раз площадь может превышать номинал

STAMP_W_MM, STAMP_H_MM = 185.0, 55.0    # основная надпись по ГОСТ 2.104
FIELD_MM = 5.0                          # поле рамки справа и снизу
STAMP_SLACK_MM = 10.0                   # запас на перекос скана
PROBE_DPI = 100                         # хватает, чтобы увидеть линии рамки
FRAME_COVER = 0.5                       # линия рамки тянется хотя бы на столько листа

# Лист спецификации — сплошная таблица во всю рамку, и опознаётся она двумя
# независимыми признаками; хватает любого.
#
# 1. **Строки таблицы.** Считаются не по всей ширине сразу, а по четырём
#    вертикальным полосам: скан перекошен, и линия, ровная в пределах четверти
#    листа, по всей ширине уже уходит вниз на десяток пикселей. Берётся
#    наименьший счёт из четырёх — он не зависит от того, где на листе пусто.
# 2. **Разделители граф по ГОСТ 2.106.** На A4 таблица шириной 185 мм совпадает
#    с рамкой, и её колонки стоят на 6, 12, 20, 90, 153 и 163 мм от левого края.
#    Рисунок ищется со сдвигом: у скана таблица бывает смещена на сантиметр.
#
# Замер по 29 листам трёх комплектов: у спецификаций строк 31–36 либо все
# 6 граф на месте, у чертежей строк не больше 8 и граф не больше 3.
SPEC_ROWS = 20                  # строк таблицы в самой пустой полосе листа
SPEC_COLUMNS = 5                # совпавших разделителей граф из шести
SPEC_COLUMNS_MM = (6.0, 12.0, 20.0, 90.0, 153.0, 163.0)
COLUMN_TOLERANCE_MM = 3.0
COLUMN_SHIFT_MM = 20            # на столько таблица бывает сдвинута на скане
ROW_STRIPS = 4
ROW_COVER, COLUMN_COVER = 0.8, 0.7
LINE_BRIDGE_MM = 20.0           # разрыв в выцветшей линии, который сшиваем

# Замеры по четырём прогонам: около 1,5 с и $0,00015 на надпись, надписей
# 10–15 на мегапиксель. Оценка грубая, и в интерфейсе показывается как «≈».
SECONDS_PER_MPIX = 18.0
USD_PER_MPIX = 0.0015


def page_size_mm(page) -> tuple[float, float]:
    return page.rect.width / MM, page.rect.height / MM


def page_format(page) -> str:
    w, h = page_size_mm(page)
    area = w * h
    for name, fw, fh in FORMATS:
        if area < fw * fh * FORMAT_TOLERANCE:
            return name
    return "больше A0"


def megapixels(page, dpi: int = DPI) -> float:
    w, h = page_size_mm(page)
    return (w / 25.4 * dpi) * (h / 25.4 * dpi) / 1e6


def estimate(mpix: float) -> tuple[int, float]:
    """Сколько примерно займёт карта обмера по листу: секунды и доллары."""
    return int(mpix * SECONDS_PER_MPIX), round(mpix * USD_PER_MPIX, 4)


def page_lines(page, work: Path, dpi: int):
    """Маска длинных линий страницы: рамка, графы штампа, строки таблиц."""
    work.mkdir(parents=True, exist_ok=True)
    png = work / "probe.png"
    page.get_pixmap(dpi=dpi).save(str(png))
    try:
        gray = load_gray(png)
    except SystemExit:
        return None
    finally:
        png.unlink(missing_ok=True)
    bw = binarize(gray)
    text_h = estimate_text_height(bw)
    return remove_long_lines(bw, max(8, int(text_h * 2.2)))[1]


def frame_corner(page, work: Path, dpi: int = PROBE_DPI) -> tuple[float, float] | None:
    """Правый нижний угол рамки чертежа, в пунктах.

    Отсчитывать штамп от края страницы нельзя: на сканах чертёж меньшего
    формата бывает переснят на A4 со случайными полями, и кроп по ГОСТ
    приходит в пустое место — модель тогда читает то, чего нет.
    Рамку ищем по мелкому рендеру: линии видно и в ста точках на дюйм.
    """
    lines = page_lines(page, work, dpi)
    if lines is None:
        return None
    bridge = max(3, int(LINE_BRIDGE_MM / 25.4 * dpi))
    columns = sheet_zones.long_runs(
        cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((bridge, 1), np.uint8)),
        0, FRAME_COVER)
    rows = sheet_zones.long_runs(
        cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((1, bridge), np.uint8)),
        1, FRAME_COVER)
    if not columns or not rows:
        return None
    scale = 72 / dpi
    return columns[-1] * scale, rows[-1] * scale


def stamp_clip(page, corner: tuple[float, float] | None = None,
               slack_mm: float = STAMP_SLACK_MM) -> pymupdf.Rect:
    """Прямоугольник основной надписи в правом нижнем углу рамки."""
    r = page.rect
    x1, y1 = corner or (r.x1 - FIELD_MM * MM, r.y1 - FIELD_MM * MM)
    return pymupdf.Rect(
        x1 - (STAMP_W_MM + slack_mm) * MM,
        y1 - (STAMP_H_MM + slack_mm) * MM,
        x1 + slack_mm * MM,
        y1 + slack_mm * MM,
    ) & r


def render_stamp(page, out: Path, corner=None, dpi: int = DPI) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    page.get_pixmap(dpi=dpi, clip=stamp_clip(page, corner)).save(str(out))
    return out


def has_text_layer(page) -> bool:
    return len(page.get_text("words")) > 40


def is_spec_by_text(page) -> bool:
    """Векторный лист: шапка спецификации читается из текстового слоя даром."""
    words = {w[4] for w in page.get_text("words")}
    return len({h for h in SPEC_HEADER if h in words}) >= 4


def _table_rows(band, bridge: int) -> int:
    """Строк таблицы в самой пустой из вертикальных полос листа."""
    closed = cv2.morphologyEx(band, cv2.MORPH_CLOSE,
                              np.ones((1, bridge), np.uint8))
    width = closed.shape[1] // ROW_STRIPS
    if width < 1:
        return 0
    return min(len(sheet_zones.long_runs(closed[:, i * width:(i + 1) * width],
                                         1, ROW_COVER))
               for i in range(ROW_STRIPS))


def _gost_columns(band, bridge: int) -> int:
    """Сколько разделителей граф по ГОСТ 2.106 стоит на своих местах."""
    closed = cv2.morphologyEx(band, cv2.MORPH_CLOSE,
                              np.ones((bridge, 1), np.uint8))
    found = [x / PX_MM
             for x in sheet_zones.long_runs(closed, 0, COLUMN_COVER)]
    if not found:
        return 0
    return max(
        sum(1 for want in SPEC_COLUMNS_MM
            if any(abs(x - want - shift) <= COLUMN_TOLERANCE_MM for x in found))
        for shift in range(-COLUMN_SHIFT_MM, COLUMN_SHIFT_MM + 1)
    )


def is_spec_by_raster(page, work: Path, dpi: int = DPI) -> bool:
    """Скан: внутри рамки стоит таблица во весь лист, а не проекции детали."""
    lines = page_lines(page, work, dpi)
    if lines is None:
        return False
    x0, y0, x1, y1 = sheet_zones.find_frame(lines)
    band = lines[y0:y1, x0:x1]
    bridge = max(3, int(LINE_BRIDGE_MM * PX_MM))
    return (_table_rows(band, bridge) >= SPEC_ROWS
            or _gost_columns(band, bridge) >= SPEC_COLUMNS)


def sheet_kind(page, work: Path) -> str:
    if has_text_layer(page):
        return "spec" if is_spec_by_text(page) else "drawing"
    # Спецификацию по ГОСТ 2.106 выполняют только на A4 — крупные листы
    # разбирать растром не нужно, а это самая долгая часть оглавления.
    if page_format(page) != "A4":
        return "drawing"
    return "spec" if is_spec_by_raster(page, work) else "drawing"


def looks_like_designation(text: str) -> bool:
    """Обозначение по ГОСТ 2.201 — это буквы с цифрами, а не одно слово.

    Проверка нужна не ради красоты: когда кроп попал мимо штампа, модель
    всё равно что-нибудь прочитает, и в оглавлении появится уверенная чушь.
    """
    core = text.strip()
    return len(core) >= 4 and any(c.isdigit() for c in core)


def index(pdf: Path, work: Path, stamp_reader=None, progress=None) -> dict:
    """Оглавление комплекта.

    `stamp_reader(png) -> dict` читает основную надпись; без него оглавление
    строится бесплатно, но без обозначений и наименований.
    """
    doc = pymupdf.open(str(pdf))
    sheets: list[dict] = []
    seen: dict[str, int] = {}                       # обозначение → первый лист

    for number, page in enumerate(doc, start=1):
        if progress:
            progress(f"лист {number} из {len(doc)}")
        w, h = page_size_mm(page)
        mpix = megapixels(page)
        seconds, usd = estimate(mpix)
        item = {
            "page": number,
            "format": page_format(page),
            "width_mm": round(w), "height_mm": round(h),
            "kind": sheet_kind(page, work),
            "designation": "", "title": "", "doc_type": "",
            "sheet": "", "sheets": "",
            "stamp_ok": True,
            "continuation": False,
            "megapixels": round(mpix, 1),
            "seconds": seconds, "usd": usd,
        }

        if item["kind"] == "drawing" and stamp_reader is not None:
            corner = frame_corner(page, work)
            if corner is None:
                # Рамку не видно — значит неизвестно, где на странице штамп.
                # Лист бывает переснят повёрнутым или обрезанным; читать наугад
                # хуже, чем не читать: в оглавление попадёт уверенная ошибка.
                item["stamp_ok"] = False
            else:
                png = render_stamp(page, work / f"stamp-{number}.png", corner)
                stamp = stamp_reader(png) or {}
                png.unlink(missing_ok=True)
                for key in ("designation", "title", "doc_type", "sheet", "sheets"):
                    item[key] = str(stamp.get(key, "")).strip()
                item["stamp_ok"] = looks_like_designation(item["designation"])
                if not item["stamp_ok"]:
                    item.update(designation="", title="", doc_type="",
                                sheet="", sheets="")

        key = item["designation"].replace(" ", "").upper()
        if item["kind"] == "drawing":
            # Продолжение многолистового документа: либо графа «Лист» больше
            # единицы, либо это обозначение уже встречалось раньше в файле.
            by_graph = item["sheet"].strip().strip(".") not in ("", "0", "1")
            item["continuation"] = bool(by_graph or (key and key in seen))
            if key and key not in seen:
                seen[key] = number
        item["suggest"] = item["kind"] == "drawing" and not item["continuation"]
        sheets.append(item)

    doc.close()

    # У продолжения графа наименования пустая — подписываем его именем первого
    # листа документа, иначе в оглавлении строка выглядит потерянной.
    first_of: dict[str, dict] = {}
    for item in sheets:
        key = item["designation"].replace(" ", "").upper()
        if not key:
            continue
        if key not in first_of:
            first_of[key] = item
        elif not item["title"]:
            item["title"] = first_of[key]["title"]

    picked = [s for s in sheets if s["suggest"]]
    return {
        "file": pdf.name,
        "pages": len(sheets),
        "dpi": DPI,
        "sheets": sheets,
        "totals": {
            "drawings": sum(1 for s in sheets if s["kind"] == "drawing"),
            "specs": sum(1 for s in sheets if s["kind"] == "spec"),
            "continuations": sum(1 for s in sheets if s["continuation"]),
            "suggested": len(picked),
            "seconds": sum(s["seconds"] for s in picked),
            "usd": round(sum(s["usd"] for s in picked), 3),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Оглавление многостраничного комплекта")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--no-stamps", action="store_true",
                    help="не читать штампы моделью — быстро и бесплатно")
    args = ap.parse_args()

    work = Path(__file__).resolve().parent.parent / "work" / "index"
    reader = None
    if not args.no_stamps:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.agents.reader import read_stamp_image   # noqa: PLC0415
        from app.llm import LLM                          # noqa: PLC0415
        llm = LLM()
        reader = lambda png: read_stamp_image(llm, png)  # noqa: E731

    result = index(args.pdf, work, reader, progress=lambda s: print(s, flush=True))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    t = result["totals"]
    for s in result["sheets"]:
        mark = "спец" if s["kind"] == "spec" else ("прод" if s["continuation"] else "  + ")
        name = f"{s['designation']} {s['title']}".strip() or "—"
        page = f"{s['sheet']}/{s['sheets']}" if s["sheets"] else ""
        print(f"{mark} {s['page']:>3} {s['format']:>7} {name[:46]:<46} {page}")
    print(f"\nлистов {result['pages']}: чертежей {t['drawings']}, "
          f"спецификаций {t['specs']}, продолжений {t['continuations']}")
    print(f"к работе предложено {t['suggested']}: "
          f"≈ {t['seconds'] // 60} мин, ≈ ${t['usd']:.2f}")


if __name__ == "__main__":
    main()
