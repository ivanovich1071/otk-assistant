"""Выполнение задания по стадиям.

Оба режима идут по одному ядру `tools/`. Разница в том, откуда берутся надписи:
у векторного PDF — из текстового слоя (бесплатно и точно), у скана — через
компьютерное зрение и vision-модель.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_docx                       # noqa: E402
import build_tz_docx                    # noqa: E402
import check_markup                     # noqa: E402
import crop                             # noqa: E402
import detect_text                      # noqa: E402
import pdf_to_image                     # noqa: E402
import sheet_index                      # noqa: E402
import vector_extract                   # noqa: E402
from tz_assemble import assemble        # noqa: E402
from tz_canon import apply as canon     # noqa: E402

from app.jobs import Job                # noqa: E402

# Проверено сквозным прогоном на реальном чертеже: во всех этих форматах
# детектор находит одни и те же надписи.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff",
                  ".bmp", ".webp", ".gif"}
BAD_IN_NAME = re.compile(r'[\\/:*?"<>|]')


def safe_name(name: str) -> str:
    return BAD_IN_NAME.sub("-", name).strip()


class Cancelled(RuntimeError):
    """Пользователь нажал «Прервать». Уже готовые файлы остаются."""


def _stop(job: Job) -> None:
    if job.cancelled:
        raise Cancelled()


def _source(job: Job) -> Path:
    """Исходник задания. У листа комплекта он лежит в папке родителя."""
    from app.jobs import load
    owner = job
    if job.parent:
        owner = load(job.parent) or job
    return owner.dir / "source" / job.source


def _publish(job: Job, path: Path) -> None:
    """Результат кладётся и в задание, и в папку, выбранную в настройках.

    У листа комплекта — в подпапку с именем файла: сорок карт от одного
    комплекта не должны перемешиваться с одиночными чертежами.
    """
    from app import settings
    out = job.dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    if path.parent != out:
        shutil.copy2(path, out / path.name)
    try:
        folder = settings.output_dir()
        if job.folder:
            folder = folder / job.folder
            folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, folder / path.name)
    except OSError as error:
        job.warnings.append(f"Не удалось скопировать в папку результатов: {error}")
    if path.name not in job.files:
        job.files.append(path.name)


def is_komplekt(job: Job) -> bool:
    """Многостраничный PDF, загруженный целиком, — комплект, а не чертёж."""
    if job.mode != "karta" or job.parent:
        return False
    source = _source(job)
    if source.suffix.lower() != ".pdf" or not source.exists():
        return False
    import pymupdf                                  # noqa: PLC0415
    with pymupdf.open(str(source)) as doc:
        return len(doc) > 1


def run_index(job: Job) -> None:
    """Оглавление комплекта. Карты обмера не делаются — ждём выбора листов."""
    source = _source(job)

    job.start("Разбор комплекта")

    def note(line: str) -> None:
        _stop(job)
        job.stage("Разбор комплекта").note = line
        job.save()

    stamp_reader = None
    try:
        from app.agents.reader import read_stamp_image   # noqa: PLC0415
        from app.llm import LLM                          # noqa: PLC0415
        llm = LLM()
        stamp_reader = lambda png: read_stamp_image(llm, png)   # noqa: E731
    except Exception as error:                      # noqa: BLE001
        job.warnings.append(
            f"Штампы листов не прочитаны: {error}. Оглавление собрано по "
            "форматам и типам листов, без обозначений и наименований."
        )

    data = sheet_index.index(source, job.dir / "index", stamp_reader, note)
    (job.dir / "sheets.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    job.sheets = data["pages"]
    totals = data["totals"]
    job.finish("Разбор комплекта",
               f"листов {data['pages']}: чертежей {totals['drawings']}, "
               f"спецификаций {totals['specs']}, продолжений {totals['continuations']}")

    first = next((s for s in data["sheets"] if s["kind"] == "drawing"), None)
    if first and (first["designation"] or first["title"]):
        job.title = f"{first['designation']} {first['title']}".strip()
    job.warnings.append(
        f"Комплект разобран, но ничего не посчитано. Отметьте листы во вкладке "
        f"«Листы» и нажмите «Разбить на листы»: предложено "
        f"{totals['suggested']} листов, это примерно {totals['seconds'] // 60} мин "
        f"и ${totals['usd']:.2f}."
    )


def run_tz(job: Job) -> None:
    source = _source(job)
    job.start("Чтение PDF")
    if source.suffix.lower() != ".pdf" or not vector_extract.has_text_layer(source):
        raise RuntimeError(
            "Режим ТЗ работает только с векторным PDF комплекта — таким, где есть "
            "текстовый слой со штампами и спецификациями. У этого файла его нет: "
            "это скан или картинка. Переключите режим на «Карта обмера» и запустите "
            "заново — она работает с любым растром."
        )
    vector = vector_extract.extract(source)
    (job.dir / "vector.json").write_text(
        json.dumps(vector, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = [p["kind"] for p in vector["pages"]]
    job.finish("Чтение PDF",
               f"листов {len(kinds)}: чертежей {kinds.count('drawing')}, "
               f"спецификаций {kinds.count('spec')}")

    job.start("Дерево комплекта")
    tz = assemble(vector)
    job.finish("Дерево комплекта",
               f"{tz['designation']} {tz['title']}".strip())

    job.start("Канон и общие данные")
    tz = canon(tz)
    try:
        from app.agents.tz_writer import write
        tz = write(tz)
        note = "модель" if tz.get("general") else "без модели"
    except Exception as error:                      # noqa: BLE001
        tz["warnings"].append(f"Шаг модели пропущен: {error}")
        note = "без модели"
    (job.dir / "tz.json").write_text(
        json.dumps(tz, ensure_ascii=False, indent=1), encoding="utf-8")
    job.finish("Канон и общие данные", note)

    job.start("Документ")
    name = safe_name(" ".join(x for x in (tz["designation"], tz["title"]) if x))
    out = job.dir / "out" / f"{name} (ТЗ).docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    build_tz_docx.build(tz, out)
    _publish(job, out)
    job.finish("Документ", out.name)

    job.title = f"{tz['designation']} {tz['title']}".strip()
    job.warnings = list(tz["warnings"])


def run_karta(job: Job) -> None:
    source = _source(job)

    job.start("Подготовка листа")
    if source.suffix.lower() == ".pdf":
        image = job.dir / "drawing.png"
        page = max(0, job.page - 1)         # 0 — обычное задание по одному листу
        info = pdf_to_image.convert(source, image, dpi=300, page=page)
        vectorish = info["vector_words"] > 20
        job.finish("Подготовка листа",
                   f"лист {page + 1} из {info['pages']}, "
                   f"{'векторный' if vectorish else 'скан'}, 300 dpi")
    elif source.suffix.lower() in IMAGE_SUFFIXES:
        image = job.dir / f"drawing{source.suffix.lower()}"
        shutil.copy2(source, image)
        job.finish("Подготовка листа", "растр как есть")
    else:
        raise RuntimeError(f"Не понимаю формат {source.suffix}")

    _stop(job)
    job.start("Поиск надписей")
    blocks: dict = detect_text.detect(image)
    (job.dir / "blocks.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    job.finish("Поиск надписей",
               f"блоков {len(blocks['blocks'])}, кегль ~{blocks['pitch']} px")

    _stop(job)
    job.start("Контактные листы")
    gray = crop.load_gray(image)
    sheets = crop.make_sheets(gray, blocks["blocks"], job.dir / "sheets",
                              per_sheet=24, scale=2)
    job.finish("Контактные листы", f"{len(sheets)} шт.")

    markup_path = job.dir / "markup.json"
    if not markup_path.exists():
        job.start("Чтение и разметка")
        try:
            from app.agents.reader import run as read_drawing
            def note(line: str) -> None:
                _stop(job)
                job.stage("Чтение и разметка").note = line
                job.save()
            markup, llm = read_drawing(image, blocks, sheets, job.dir / "sheets", note,
                                       hint=Path(job.source).stem)
        except Cancelled:
            raise
        except Exception as error:                  # noqa: BLE001
            job.warnings.append(
                f"Чтение надписей не выполнено: {error}. Контактные листы готовы "
                f"({len(sheets)} шт.) — можно разметить чертёж скиллом /razmetka "
                "в Claude Code, положить markup.json рядом с заданием и нажать «Повторить»."
            )
            for name in ("Чтение и разметка", "Нумерация", "Карта обмера"):
                job.stage(name).note = "ждёт разметку"
                job.stage(name).status = "queued"
            job.save()
            return
        markup_path.write_text(json.dumps(markup, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        positions = sum(len(g["items"]) for g in markup["groups"])
        job.finish("Чтение и разметка",
                   f"позиций {positions} в {len(markup['groups'])} группах, "
                   f"{llm.usage.summary()}")
        job.warnings.append(
            "Разбивка по видам и порядок номеров расставлены автоматически "
            "и требуют просмотра: на сканах группы иногда слипаются. "
            "Значения позиций прочитаны моделью — сверьте сомнительные."
        )

    markup = json.loads(markup_path.read_text(encoding="utf-8"))
    _finish_karta(job, image, markup, blocks)


def _finish_karta(job: Job, image: Path, markup: dict, blocks: dict) -> None:
    """Нумерация, карта и проверка. Отдельно — чтобы повторять после правки."""
    job.start("Нумерация")
    name = safe_name(" ".join(
        x for x in (markup.get("designation"), markup.get("title")) if x))
    if not name:
        name = safe_name(Path(job.source).stem)     # штамп не прочитан — имя по файлу
    marked = job.dir / "out" / f"{name} (карта обмера).jpg"
    marked.parent.mkdir(parents=True, exist_ok=True)
    import render_numbers                       # noqa: PLC0415
    count = render_numbers.render(image, markup, marked)
    # Куда сел каждый номер, знает только render_numbers. Без записи обратно
    # номер нельзя ни подвинуть мышкой, ни перерисовать на прежнем месте.
    (job.dir / "markup.json").write_text(
        json.dumps(markup, ensure_ascii=False, indent=1), encoding="utf-8")
    _publish(job, marked)
    job.finish("Нумерация", f"номеров {count}")

    job.start("Карта обмера")
    card = job.dir / "out" / f"{name} (карта обмера).docx"
    rows = build_docx.build(markup, card)
    _publish(job, card)
    job.finish("Карта обмера", f"строк {rows}")

    job.start("Проверка")
    problems = check_markup.check(markup, blocks)
    job.warnings.extend(problems)
    job.finish("Проверка", "замечаний нет" if not problems else f"замечаний {len(problems)}")
    job.title = name


def run(job_id: str) -> None:
    import time
    from app.jobs import load
    job = load(job_id)
    if job is None:
        return
    if job.cancelled:               # прервали, пока задание стояло в очереди
        job.stop()
        return
    job.status = "running"
    job.started = time.time()
    job.finished = 0.0
    job.save()
    try:
        if job.mode == "tz":
            run_tz(job)
        elif is_komplekt(job):
            run_index(job)
        else:
            run_karta(job)
        job.status = "done"
        job.finished = time.time()
        job.save()
    except Cancelled:
        job.stop()
    except Exception as error:                      # noqa: BLE001
        job.fail(str(error))


def rebuild(job_id: str) -> None:
    """Пересборка результата после правки разметки — без обращения к модели."""
    import time
    from app.jobs import load
    job = load(job_id)
    if job is None or job.mode != "karta":
        return
    job.status = "running"
    job.started = time.time()
    job.finished = 0.0
    job.files = []
    job.warnings = []
    job.save()
    try:
        image = next((p for p in job.dir.glob("drawing.*")), None)
        markup = json.loads((job.dir / "markup.json").read_text(encoding="utf-8"))
        blocks = json.loads((job.dir / "blocks.json").read_text(encoding="utf-8"))
        _finish_karta(job, image, markup, blocks)
        job.status = "done"
        job.finished = time.time()
        job.save()
    except Exception as error:                      # noqa: BLE001
        job.fail(str(error))
