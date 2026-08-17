"""Кропы текстовых блоков чертежа.

Два режима:
  sheets — контактные листы: десятки надписей одной картинкой с подписанными
           номерами. Агент читает 10-15 листов вместо сотен отдельных файлов.
  crops  — отдельные файлы (для вставки рамок допусков в Word и для перепроверки
           спорных позиций контролёром).

Использование:
    python tools/crop.py <чертёж> --blocks work/blocks.json sheets -o work/sheets
    python tools/crop.py <чертёж> --blocks work/blocks.json crops  -o work/crops --ids 12,45
    python tools/crop.py <чертёж> crops -o work/frames --box 1520,300,180,60
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


PAD = 6            # запас вокруг bbox, чтобы не срезать засечки
ALONG_PAD = 1.3    # запас вдоль строки в долях кегля — под срезанные Ø, R, M
SCALE = 3          # апскейл: мелкий шрифт чертежа читается только увеличенным
CELL_GAP = 10
LABEL_H = 22
SHEET_W = 1500     # ширина контактного листа в пикселях


def load_gray(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit(f"Не удалось прочитать изображение: {path}")
    return img


def save(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise SystemExit(f"Не удалось закодировать {path}")
    path.write_bytes(buf.tobytes())


def cut(gray: np.ndarray, box: tuple[int, int, int, int], angle: int = 0,
        pad: int = PAD, scale: int = SCALE) -> np.ndarray:
    """Кроп надписи с запасом вдоль строки.

    Знак Ø — перечёркнутый кружок — не проходит фильтр заливки в `detect_text`
    и в рамку блока не попадает. Кроп по голой рамке резал его пополам, и модель
    читала `Ø1200` как `1200`. Поэтому вдоль строки запас больше кегля,
    а поперёк остаётся прежним, чтобы не втянуть соседнюю надпись.
    """
    x, y, w, h = box
    across = w if angle in (90, 270) else h
    along = max(pad, int(across * ALONG_PAD))
    pad_y, pad_x = (along, pad) if angle in (90, 270) else (pad, along)

    y0, y1 = max(0, y - pad_y), min(gray.shape[0], y + h + pad_y)
    x0, x1 = max(0, x - pad_x), min(gray.shape[1], x + w + pad_x)
    piece = gray[y0:y1, x0:x1]
    if angle == 90:
        # На чертежах по ГОСТ повёрнутый текст читается снизу вверх.
        piece = cv2.rotate(piece, cv2.ROTATE_90_CLOCKWISE)
    if scale != 1:
        piece = cv2.resize(piece, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    return piece


def _labelled(piece: np.ndarray, text: str) -> np.ndarray:
    """Кроп с подписью-номером сверху, чтобы агент не перепутал блоки местами."""
    h, w = piece.shape[:2]
    w = max(w, 90)
    canvas = np.full((h + LABEL_H, w), 255, np.uint8)
    canvas[LABEL_H : LABEL_H + h, : piece.shape[1]] = piece
    canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    cv2.putText(canvas, text, (2, LABEL_H - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 200), 1,
                cv2.LINE_AA)
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1] - 1, canvas.shape[0] - 1), (170, 170, 170), 1)
    return canvas


def make_sheets(gray: np.ndarray, blocks: list[dict], out_dir: Path,
                per_sheet: int, scale: int) -> list[dict]:
    cells = [
        (b["id"], _labelled(cut(gray, (b["x"], b["y"], b["w"], b["h"]), b["angle"], scale=scale),
                            f"#{b['id']}"))
        for b in blocks
    ]

    sheets: list[dict] = []
    for start in range(0, len(cells), per_sheet):
        chunk = cells[start : start + per_sheet]
        rows: list[list[tuple[int, np.ndarray]]] = [[]]
        width = 0
        for cid, img in chunk:
            cw = img.shape[1] + CELL_GAP
            if width + cw > SHEET_W and rows[-1]:
                rows.append([])
                width = 0
            rows[-1].append((cid, img))
            width += cw

        row_imgs = []
        for row in rows:
            rh = max(img.shape[0] for _, img in row)
            rw = sum(img.shape[1] + CELL_GAP for _, img in row) + CELL_GAP
            canvas = np.full((rh + CELL_GAP, rw, 3), 255, np.uint8)
            x = CELL_GAP
            for _, img in row:
                canvas[0 : img.shape[0], x : x + img.shape[1]] = img
                x += img.shape[1] + CELL_GAP
            row_imgs.append(canvas)

        sheet_w = max(r.shape[1] for r in row_imgs)
        sheet = np.full((sum(r.shape[0] for r in row_imgs), sheet_w, 3), 255, np.uint8)
        y = 0
        for r in row_imgs:
            sheet[y : y + r.shape[0], 0 : r.shape[1]] = r
            y += r.shape[0]

        name = f"sheet_{len(sheets) + 1:02d}.png"
        save(out_dir / name, sheet)
        sheets.append({"file": name, "ids": [cid for cid, _ in chunk]})
    return sheets


def main() -> None:
    ap = argparse.ArgumentParser(description="Кропы надписей чертежа")
    ap.add_argument("image", type=Path)
    ap.add_argument("--blocks", type=Path, help="work/blocks.json")
    ap.add_argument("mode", choices=["sheets", "crops"])
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--ids", help="только эти id блоков, через запятую")
    ap.add_argument("--box", help="произвольная область x,y,w,h (без --blocks)")
    ap.add_argument("--per-sheet", type=int, default=24)
    ap.add_argument("--scale", type=int, default=SCALE)
    ap.add_argument("--pad", type=int, default=PAD)
    args = ap.parse_args()

    gray = load_gray(args.image)

    if args.box:
        x, y, w, h = (int(v) for v in args.box.split(","))
        save(args.out, cut(gray, (x, y, w, h), 0, pad=args.pad, scale=args.scale))
        print(f"-> {args.out}")
        return

    if not args.blocks:
        raise SystemExit("нужен --blocks или --box")
    data = json.loads(args.blocks.read_text(encoding="utf-8"))
    blocks = data["blocks"]
    if args.ids:
        wanted = {int(v) for v in args.ids.split(",")}
        blocks = [b for b in blocks if b["id"] in wanted]

    if args.mode == "sheets":
        sheets = make_sheets(gray, blocks, args.out, args.per_sheet, args.scale)
        (args.out / "index.json").write_text(
            json.dumps({"sheets": sheets}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"листов: {len(sheets)}, надписей: {len(blocks)} -> {args.out}")
    else:
        for b in blocks:
            piece = cut(gray, (b["x"], b["y"], b["w"], b["h"]), b["angle"],
                        pad=args.pad, scale=args.scale)
            save(args.out / f"block_{b['id']:04d}.png", piece)
        print(f"кропов: {len(blocks)} -> {args.out}")


if __name__ == "__main__":
    main()
