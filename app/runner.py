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


def _source(job: Job) -> Path:
    return job.dir / "source" / job.source


def _publish(job: Job, path: Path) -> None:
    """Результат кладётся и в задание, и в папку, выбранную в настройках."""
    from app import settings
    out = job.dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    if path.parent != out:
        shutil.copy2(path, out / path.name)
    try:
        shutil.copy2(path, settings.output_dir() / path.name)
    except OSError as error:
        job.warnings.append(f"Не удалось скопировать в папку результатов: {error}")
    if path.name not in job.files:
        job.files.append(path.name)


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
        info = pdf_to_image.convert(source, image, dpi=300, page=0)
        vectorish = info["vector_words"] > 20
        job.finish("Подготовка листа",
                   f"{info['pages']} стр., {'векторный' if vectorish else 'скан'}, 300 dpi")
        if info["pages"] > 1:
            job.warnings.append(
                f"В PDF {info['pages']} страниц — карта обмера делается по первой. "
                "Для остальных листов загрузите их отдельными заданиями."
            )
    elif source.suffix.lower() in IMAGE_SUFFIXES:
        image = job.dir / f"drawing{source.suffix.lower()}"
        shutil.copy2(source, image)
        job.finish("Подготовка листа", "растр как есть")
    else:
        raise RuntimeError(f"Не понимаю формат {source.suffix}")

    job.start("Поиск надписей")
    blocks: dict = detect_text.detect(image)
    (job.dir / "blocks.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    job.finish("Поиск надписей",
               f"блоков {len(blocks['blocks'])}, высота шрифта ~{blocks['text_height']} px")

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
                job.stage("Чтение и разметка").note = line
                job.save()
            markup, llm = read_drawing(image, blocks, sheets, job.dir / "sheets", note)
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
    job.status = "running"
    job.started = time.time()
    job.finished = 0.0
    job.save()
    try:
        (run_tz if job.mode == "tz" else run_karta)(job)
        job.status = "done"
        job.finished = time.time()
        job.save()
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
