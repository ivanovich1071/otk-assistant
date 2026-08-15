"""Настройки приложения.

Пока настройка одна — куда складывать готовые файлы. В веб-версии путь вводится
руками, в десктопной его подставит системный диалог выбора папки; поле и логика
у них общие, поэтому настройка живёт здесь, а не в интерфейсе.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "work" / "settings.json"
DEFAULT_OUTPUT = ROOT / "output"


def read() -> dict:
    data = {"output_dir": str(DEFAULT_OUTPUT)}
    if FILE.exists():
        try:
            data.update(json.loads(FILE.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return data


def write(values: dict) -> dict:
    data = read()
    if "output_dir" in values:
        folder = Path(str(values["output_dir"]).strip() or DEFAULT_OUTPUT)
        folder.mkdir(parents=True, exist_ok=True)
        data["output_dir"] = str(folder)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def output_dir() -> Path:
    folder = Path(read()["output_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    return folder
