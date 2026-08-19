"""Задания на диске.

Задание живёт в `work/jobs/<id>/`: исходный файл, промежуточные json, результат
и журнал стадий. Приложение можно закрыть и вернуться — ничего не теряется,
а любую стадию видно по журналу.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "work" / "jobs"

MODES = {"karta": "Карта обмера", "tz": "Требования по изготовлению"}
STATUS = {"queued": "в очереди", "running": "выполняется",
          "done": "готово", "failed": "ошибка", "cancelled": "прервано"}

# Обработчик пишет журнал стадий, а интерфейс опрашивает его раз в секунду —
# оба в одном процессе. Без замка читатель успевал попасть между обрезкой файла
# и записью и падал на пустом JSON.
_lock = threading.RLock()


def _write_json(path: Path, data: dict) -> None:
    """Запись через временный файл, с оглядкой на Windows.

    `os.replace` здесь атомарна, но падает с WinError 5, если файл в этот момент
    кто-то держит открытым — обычно антивирус. Тогда после нескольких попыток
    пишем напрямую: под замком читателей из приложения всё равно нет.
    """
    text = json.dumps(data, ensure_ascii=False, indent=1)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    path.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


@dataclass
class Stage:
    name: str
    status: str = "queued"
    note: str = ""
    seconds: float = 0.0


@dataclass
class Job:
    id: str
    mode: str
    source: str
    title: str = ""
    status: str = "queued"
    # Комплект: у листа стоит номер страницы и ссылка на задание-комплект,
    # исходный PDF лежит у родителя и второй раз не копируется.
    page: int = 0
    parent: str = ""
    folder: str = ""
    sheets: int = 0
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    stages: list[Stage] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def dir(self) -> Path:
        return JOBS_DIR / self.id

    def save(self) -> None:
        with _lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            _write_json(self.dir / "job.json", asdict(self))

    def stage(self, name: str) -> Stage:
        for item in self.stages:
            if item.name == name:
                return item
        item = Stage(name)
        self.stages.append(item)
        return item

    def start(self, name: str) -> Stage:
        item = self.stage(name)
        item.status = "running"
        item.seconds = time.time()
        self.save()
        return item

    def finish(self, name: str, note: str = "") -> None:
        item = self.stage(name)
        item.status = "done"
        item.note = note
        item.seconds = round(time.time() - item.seconds, 1)
        self.save()

    def fail(self, message: str) -> None:
        for item in self.stages:
            if item.status == "running":
                item.status = "failed"
        self.status = "failed"
        self.error = message
        self.finished = time.time()
        self.save()

    @property
    def cancel_flag(self) -> Path:
        return self.dir / "cancel"

    def cancel(self) -> None:
        """Просьба остановиться.

        Файлом, а не полем: её ставит поток интерфейса, а читает поток
        обработки, и переживать перезапуск приложения она тоже должна.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cancel_flag.write_text("", encoding="utf-8")
        if self.status == "queued":
            self.status = "cancelled"
            self.finished = time.time()
            self.save()

    @property
    def cancelled(self) -> bool:
        return self.cancel_flag.exists()

    def stop(self) -> None:
        """Отметить задание прерванным. Уже готовые файлы остаются."""
        for item in self.stages:
            if item.status == "running":
                item.status = "failed"
                item.note = "прервано"
        self.status = "cancelled"
        self.finished = time.time()
        self.save()

    @property
    def elapsed(self) -> float:
        """Фактическое время обработки, в секундах."""
        if not self.started:
            return 0.0
        return round((self.finished or time.time()) - self.started, 1)


def create(mode: str, filename: str, data: bytes) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], mode=mode, source=filename,
              title=Path(filename).stem)
    job.dir.mkdir(parents=True, exist_ok=True)
    (job.dir / "source" / filename).parent.mkdir(parents=True, exist_ok=True)
    (job.dir / "source" / filename).write_bytes(data)
    job.save()
    return job


def create_child(parent: Job, page: int, title: str = "", folder: str = "") -> Job:
    """Задание по одному листу комплекта.

    Исходник не копируется: 40 листов одного файла — это 40 копий по 17 МБ.
    Лист берётся из PDF родителя по номеру страницы.
    """
    job = Job(id=uuid.uuid4().hex[:12], mode=parent.mode, source=parent.source,
              title=title or f"{parent.title} — лист {page}",
              page=page, parent=parent.id, folder=folder)
    job.dir.mkdir(parents=True, exist_ok=True)
    job.save()
    return job


def load(job_id: str) -> Job | None:
    path = JOBS_DIR / job_id / "job.json"
    if not path.exists():
        return None
    with _lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["stages"] = [Stage(**s) for s in raw.get("stages", [])]
            return Job(**raw)
        except (json.JSONDecodeError, TypeError, OSError):
            # Битое задание не должно ронять список всех остальных.
            return None


def listing() -> list[Job]:
    if not JOBS_DIR.exists():
        return []
    jobs = [load(p.name) for p in JOBS_DIR.iterdir() if p.is_dir()]
    return sorted((j for j in jobs if j), key=lambda j: j.created, reverse=True)
