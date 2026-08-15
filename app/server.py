"""Локальный сервер приложения «Ассистент ОТК».

Приложение однопользовательское и локальное, поэтому без авторизации и без
внешних зависимостей у фронтенда. Состояние задания опрашивается коротким
запросом раз в секунду — этого хватает и не требует держать соединение.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from fastapi import (BackgroundTasks, FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import jobs, runner, settings    # noqa: E402
from app.runner import rebuild, run       # noqa: E402

WEB = Path(__file__).resolve().parent / "web"
app = FastAPI(title="Ассистент ОТК", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/static/{name}")
def static(name: str) -> FileResponse:
    path = WEB / name
    if not path.exists() or path.parent != WEB:
        raise HTTPException(404, "нет такого файла")
    return FileResponse(path)


@app.get("/api/state")
def state() -> dict:
    try:
        from app.llm import find_api_key
        find_api_key()
        key = True
    except Exception:                       # noqa: BLE001
        key = False
    return {"key": key, "modes": jobs.MODES,
            "output": settings.read()["output_dir"],
            "desktop": bool(getattr(app.state, "picker", None)),
            "formats": sorted(runner.IMAGE_SUFFIXES | {".pdf"})}


@app.get("/api/settings")
def settings_get() -> dict:
    return settings.read()


@app.put("/api/settings")
async def settings_put(request: Request) -> dict:
    try:
        return settings.write(await request.json())
    except OSError as error:
        raise HTTPException(400, f"папка недоступна: {error}") from error


@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Системный выбор папки. Работает только в оконной версии приложения."""
    picker = getattr(app.state, "picker", None)
    if picker is None:
        raise HTTPException(501, "выбор папки доступен в оконной версии; "
                                 "в браузере впишите путь вручную")
    folder = picker()
    if not folder:
        return settings.read()
    return settings.write({"output_dir": folder})


def _view(job: jobs.Job) -> dict:
    """Задание для интерфейса: к полям добавляется фактическое время обработки."""
    data = asdict(job)
    data["elapsed"] = job.elapsed
    return data


@app.get("/api/jobs")
def job_list() -> list[dict]:
    return [_view(j) for j in jobs.listing()]


@app.post("/api/jobs")
async def job_create(background: BackgroundTasks, mode: str = Form(...),
                     file: UploadFile = File(...)) -> dict:
    if mode not in jobs.MODES:
        raise HTTPException(400, "неизвестный режим")
    data = await file.read()
    if not data:
        raise HTTPException(400, "пустой файл")
    job = jobs.create(mode, Path(file.filename or "чертёж.pdf").name, data)
    background.add_task(run, job.id)
    return _view(job)


@app.delete("/api/jobs")
def job_clear() -> JSONResponse:
    """Очистить список: удаляются задания, готовые файлы в папке результатов остаются."""
    import shutil
    removed = 0
    for job in jobs.listing():
        if job.status in ("running", "queued"):
            continue                        # идущее задание не трогаем
        shutil.rmtree(job.dir, ignore_errors=True)
        removed += 1
    return JSONResponse({"removed": removed})


@app.post("/api/jobs/{job_id}/export")
def job_export(job_id: str) -> dict:
    """Скопировать готовые файлы задания в папку из настроек."""
    import shutil
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    folder = settings.output_dir()
    saved = []
    for name in job.files:
        source = job.dir / "out" / name
        if source.exists():
            shutil.copy2(source, folder / name)
            saved.append(name)
    return {"folder": str(folder), "saved": saved}


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str) -> dict:
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    return _view(job)


@app.post("/api/jobs/{job_id}/restart")
def job_restart(job_id: str, background: BackgroundTasks) -> dict:
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    job.stages, job.files, job.warnings, job.error = [], [], [], ""
    job.status = "queued"
    job.save()
    background.add_task(run, job.id)
    return _view(job)


@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: str) -> JSONResponse:
    import shutil
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    shutil.rmtree(job.dir, ignore_errors=True)
    return JSONResponse({"ok": True})


def _job_file(job_id: str, name: str, folder: str = "out") -> tuple[jobs.Job, Path]:
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    path = (job.dir / folder / name).resolve()
    if not path.exists() or job.dir.resolve() not in path.parents:
        raise HTTPException(404, "нет такого файла")
    return job, path


@app.get("/api/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str) -> FileResponse:
    """Скачивание: браузер сохраняет файл, а не открывает."""
    _, path = _job_file(job_id, name)
    return FileResponse(path, filename=name,
                        media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/view/{name}")
def job_view(job_id: str, name: str) -> FileResponse:
    """Просмотр: то же самое, но открывается прямо в окне."""
    _, path = _job_file(job_id, name)
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/markup")
def job_markup(job_id: str) -> dict:
    """Разметка карты обмера или разобранный комплект — для просмотра и правки."""
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    name = "markup.json" if job.mode == "karta" else "tz.json"
    path = job.dir / name
    if not path.exists():
        raise HTTPException(404, "разметки ещё нет")
    return json.loads(path.read_text(encoding="utf-8"))


@app.put("/api/jobs/{job_id}/markup")
async def job_markup_save(job_id: str, request: Request) -> dict:
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    if job.mode != "karta":
        raise HTTPException(400, "править можно только карту обмера")
    data = await request.json()
    if not isinstance(data, dict) or "groups" not in data:
        raise HTTPException(400, "ожидалась разметка с полем groups")
    (job.dir / "markup.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "positions": sum(len(g["items"]) for g in data["groups"])}


@app.post("/api/jobs/{job_id}/rebuild")
def job_rebuild(job_id: str, background: BackgroundTasks) -> dict:
    """Пересобрать чертёж и карту из правленой разметки. Модель не вызывается."""
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "задание не найдено")
    if not (job.dir / "markup.json").exists():
        raise HTTPException(400, "нет разметки")
    background.add_task(rebuild, job.id)
    return _view(job)
