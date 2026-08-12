"""Поиск рамок допусков формы и расположения.

Рамка по ГОСТ 2.308 — прямоугольник, разделённый вертикальными перегородками
на 2-3 клетки. Её содержимое (⊥, ⌭, ⌖, ∕, ↗, база) не распознаётся текстом,
а вырезается картинкой и вставляется в карту обмера — так делает и человек.

Использование:
    python tools/detect_frames.py <чертёж> -o work/frames.json [--debug work/frames.png]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from detect_text import binarize, estimate_text_height, load_gray


# Клетка рамки допуска по ГОСТ 2.308 заметно выше строки текста; строки штампа,
# наоборот, ниже. Пропорции даны в высотах шрифта чертежа.
CELL_MIN = 2.0
CELL_MAX = 3.6


def find_frames(gray: np.ndarray, text_h: float) -> list[dict]:
    bw = binarize(gray)
    min_len = max(14, int(text_h * 0.9))
    h_lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1)))
    v_lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len)))
    grid = cv2.dilate(cv2.bitwise_or(h_lines, v_lines), np.ones((3, 3), np.uint8))

    # Клетки рамки — «дырки» в сетке линий: ищем их как внутренние контуры.
    contours, hierarchy = cv2.findContours(grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []

    cells: list[tuple[int, int, int, int]] = []
    for i, cnt in enumerate(contours):
        if hierarchy[0][i][3] < 0:
            continue  # внешний контур, не полость
        x, y, w, h = cv2.boundingRect(cnt)
        if not (text_h * CELL_MIN <= h <= text_h * CELL_MAX):
            continue  # клетки штампа заметно ниже, клетки таблиц — выше
        if not (text_h * 0.4 <= w <= text_h * 9):
            continue
        if cv2.contourArea(cnt) / float(w * h) < 0.7:
            continue  # не прямоугольная полость
        cells.append((x, y, w, h))

    # Клетки одной рамки стоят вплотную: в строку (⊥|0,1|И) или в столбик.
    cells.sort(key=lambda c: (c[1], c[0]))
    used = [False] * len(cells)
    frames: list[dict] = []
    for i, (x, y, w, h) in enumerate(cells):
        if used[i]:
            continue
        chain = [i]
        cx, cy, cw, ch = x, y, w, h
        changed = True
        while changed:
            changed = False
            for j, (x2, y2, w2, h2) in enumerate(cells):
                if used[j] or j in chain:
                    continue
                same_row = (abs(y2 - cy) <= text_h * 0.45 and abs(h2 - ch) <= text_h * 0.5
                            and -text_h * 0.35 <= x2 - (cx + cw) <= text_h * 0.45)
                same_col = (abs(x2 - cx) <= text_h * 0.45 and abs(w2 - cw) <= text_h * 0.5
                            and -text_h * 0.35 <= y2 - (cy + ch) <= text_h * 0.45)
                if same_row:
                    cw = x2 + w2 - cx
                elif same_col:
                    ch = y2 + h2 - cy
                else:
                    continue
                chain.append(j)
                changed = True
        if len(chain) < 2:
            continue  # одиночная клетка — это не рамка допуска
        for j in chain:
            used[j] = True
        pad = max(2, int(text_h * 0.12))
        frames.append({
            "id": len(frames) + 1,
            "x": max(0, cx - pad), "y": max(0, cy - pad),
            "w": cw + 2 * pad, "h": ch + 2 * pad,
            "cells": len(chain),
        })

    frames.sort(key=lambda f: (f["y"], f["x"]))
    for n, f in enumerate(frames, 1):
        f["id"] = n
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description="Поиск рамок допусков на чертеже")
    ap.add_argument("image", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("work/frames.json"))
    ap.add_argument("--debug", type=Path)
    args = ap.parse_args()

    gray = load_gray(args.image)
    text_h = estimate_text_height(binarize(gray))
    frames = find_frames(gray, text_h)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"image": str(args.image), "text_height": round(text_h, 1), "frames": frames},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    if args.debug:
        canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for f in frames:
            cv2.rectangle(canvas, (f["x"], f["y"]), (f["x"] + f["w"], f["y"] + f["h"]),
                          (0, 160, 0), 3)
        ok, buf = cv2.imencode(".png", canvas)
        if ok:
            args.debug.parent.mkdir(parents=True, exist_ok=True)
            args.debug.write_bytes(buf.tobytes())

    print(f"рамок допусков: {len(frames)} -> {args.out}")


if __name__ == "__main__":
    main()
