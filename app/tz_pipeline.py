"""Сквозная сборка требований по изготовлению из комплекта чертежей.

    python -m app.tz_pipeline "input/Барабан натяжной ... СБ.pdf"

Стадии: текст из PDF → дерево комплекта → канон и «Общие данные» → docx.
Промежуточные файлы остаются в work/, чтобы любую стадию можно было
перезапустить отдельно.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from build_tz_docx import build            # noqa: E402
from tz_assemble import assemble           # noqa: E402
from tz_canon import apply as canon        # noqa: E402
from vector_extract import extract, has_text_layer   # noqa: E402

from app.agents.tz_writer import write     # noqa: E402


def run(pdf: Path, out_dir: Path, work: Path, use_model: bool = True) -> dict:
    if not has_text_layer(pdf):
        raise SystemExit(
            f"{pdf.name}: нет текстового слоя, это скан. Режим ТЗ работает "
            "только с векторными PDF — см. .claude/rules/tz-format.md")

    work.mkdir(parents=True, exist_ok=True)
    print("1/4 текст из PDF")
    vector = extract(pdf)
    (work / "vector.json").write_text(
        json.dumps(vector, ensure_ascii=False, indent=1), encoding="utf-8")

    print("2/4 дерево комплекта")
    tz = assemble(vector)

    print("3/4 канон и «Общие данные»")
    tz = canon(tz)
    if use_model:
        tz = write(tz)
    (work / "tz.json").write_text(
        json.dumps(tz, ensure_ascii=False, indent=1), encoding="utf-8")

    print("4/4 сборка документа")
    name = " ".join(x for x in (tz["designation"], tz["title"]) if x)
    # В наименованиях встречается «Ц130L210/Ц140L250» — в имени файла это путь.
    out = out_dir / (re.sub(r'[\\/:*?"<>|]', "-", name) + " (ТЗ).docx")
    build(tz, out)

    print(f"\n{name}")
    print(f"-> {out}")
    for line in tz["warnings"]:
        print("  ! " + line)
    return tz


def main() -> None:
    ap = argparse.ArgumentParser(description="Требования по изготовлению из PDF комплекта")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("output"))
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--no-model", action="store_true",
                    help="только детерминированный путь, без OpenRouter")
    args = ap.parse_args()

    run(args.pdf, args.out_dir, args.work, use_model=not args.no_model)


if __name__ == "__main__":
    main()
