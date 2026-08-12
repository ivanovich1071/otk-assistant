"""PDF -> растр для разметки.

Заодно сообщает, векторный PDF или скан: у векторного есть текстовый слой
с координатами, и его в будущем можно читать без распознавания.

    python tools/pdf_to_image.py "input/деталь.pdf" -o "input/деталь.png" [--dpi 300]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf


def convert(src: Path, out: Path, dpi: int, page: int) -> dict:
    doc = pymupdf.open(str(src))
    if page >= len(doc):
        raise SystemExit(f"В файле {len(doc)} стр., запрошена {page + 1}")
    p = doc[page]
    words = p.get_text("words")

    out.parent.mkdir(parents=True, exist_ok=True)
    p.get_pixmap(dpi=dpi).save(str(out))

    return {
        "pages": len(doc),
        "vector_words": len(words),
        "width_pt": round(p.rect.width, 1),
        "height_pt": round(p.rect.height, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF в растр для разметки")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--page", type=int, default=1, help="номер страницы, с 1")
    args = ap.parse_args()

    info = convert(args.pdf, args.out, args.dpi, args.page - 1)
    kind = ("векторный (есть текстовый слой, %d слов)" % info["vector_words"]
            if info["vector_words"] > 20 else "скан (текстового слоя нет)")
    print(f"страниц: {info['pages']}, лист {info['width_pt']}×{info['height_pt']} pt, {kind}")
    print(f"-> {args.out} @ {args.dpi} dpi")


if __name__ == "__main__":
    main()
