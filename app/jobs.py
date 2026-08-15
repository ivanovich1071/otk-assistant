"""Задания на диске.

Задание живёт в `work/jobs/<id>/`: исходный файл, промежуточные json, результат
и журнал стадий. Приложение можно закрыть и вернуться — ничего не теряется,
а любую стадию видно по журналу.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "work" / "jobs"

MODES = {"karta": "Карта обмера", "tz": "Требования по изготовлению"}
STATUS = {"queued": "в очереди", "running": "выполняется",
          "done": "готово", "failed": "ошибка"}


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
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "job.json").write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

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


def load(job_id: str) -> Job | None:
    path = JOBS_DIR / job_id / "job.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["stages"] = [Stage(**s) for s in raw.get("stages", [])]
    return Job(**raw)


def listing() -> list[Job]:
    if not JOBS_DIR.exists():
        return []
    jobs = [load(p.name) for p in JOBS_DIR.iterdir() if p.is_dir()]
    return sorted((j for j in jobs if j), key=lambda j: j.created, reverse=True)
