"""Сборка дерева комплекта из vector.json в контракт tz.json.

Источник истины по составу — спецификации: они говорят, что во что входит,
сколько штук и сколько весит. Чертежи дают технические требования, материал
и массу по штампу. Расхождения не сглаживаются, а попадают в `warnings`:
именно там ловится, например, «Крышка глухая 3,8 кг по штампу и 3,7 по спецификации».

    python tools/tz_assemble.py work/vector.json -o work/tz.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ASSEMBLY_SUFFIX = " СБ"
SECTION_UNITS = "Сборочные единицы"
SECTION_PARTS = "Детали"
SECTION_STANDARD = "Стандартные изделия"

_MASS = re.compile(r"^\s*([\d,.]+)\s*(кг)?\s*$")


def base(designation: str) -> str:
    """Обозначение без суффикса «СБ»: чертёж и его спецификация — один объект."""
    text = designation.strip()
    return text[: -len(ASSEMBLY_SUFFIX)] if text.endswith(ASSEMBLY_SUFFIX) else text


def mass_kg(text: str) -> str:
    match = _MASS.match(text or "")
    return f"{match.group(1)} кг" if match else (text or "").strip()


def _index(pages: list[dict]) -> tuple[dict, dict]:
    """Чертежи и спецификации по обозначению без «СБ»."""
    drawings: dict[str, dict] = {}
    specs: dict[str, dict] = {}
    for page in pages:
        key = base(page.get("stamp", {}).get("designation", ""))
        if not key:
            continue
        if page["kind"] == "spec":
            specs.setdefault(key, page)
        else:
            drawings.setdefault(key, page)
    return drawings, specs


def _part(row: dict, drawings: dict, warnings: list[str]) -> dict:
    key = base(row["designation"])
    page = drawings.get(key)
    spec_mass = mass_kg(row["note"])

    if page is None:
        warnings.append(
            f"{row['designation']} «{row['name']}» — в спецификации есть, чертежа "
            "в комплекте нет; данные взяты из спецификации")
        return {"designation": row["designation"], "title": row["name"], "page": None,
                "qty": row["qty"], "mass": spec_mass,
                "material": row.get("material", ""), "tech_requirements": []}

    stamp = page["stamp"]
    stamp_mass = mass_kg(stamp.get("mass", ""))
    if spec_mass and stamp_mass and spec_mass != stamp_mass:
        warnings.append(
            f"{row['designation']} «{row['name']}» — масса по штампу {stamp_mass}, "
            f"по спецификации {spec_mass}")
    return {
        "designation": row["designation"],
        "title": stamp.get("title") or row["name"],
        "page": page["page"],
        "qty": row["qty"],
        "mass": spec_mass or stamp_mass,
        "material": stamp.get("material", ""),
        "tech_requirements": page.get("tech_requirements", []),
    }


def _assembly(key: str, pos: str, level: int, drawings: dict, specs: dict,
              warnings: list[str], seen: set[str]) -> dict:
    seen.add(key)
    page = drawings.get(key)
    spec_page = specs.get(key)
    stamp = (page or spec_page or {}).get("stamp", {})
    rows = spec_page["spec"] if spec_page else []

    if spec_page is None:
        warnings.append(f"{key} — сборка без листа спецификации, состав неизвестен")

    node = {
        "designation": stamp.get("designation", key) or key,
        "title": stamp.get("title", ""),
        "level": level,
        "pos": pos,
        "page": page["page"] if page else None,
        "spec_page": spec_page["page"] if spec_page else None,
        "mass": mass_kg((page or {}).get("stamp", {}).get("mass", "")),
        "tech_requirements": (page or {}).get("tech_requirements", []),
        "spec": rows,
        "parts": [],
        "children": [],
    }

    for row in rows:
        if not row["designation"] or row["section"] == SECTION_STANDARD:
            continue
        child_key = base(row["designation"])
        if child_key == key:
            continue                      # строка «Сборочный чертеж» самого себя
        if row["section"] == SECTION_UNITS or child_key in specs:
            node["children"].append(
                _assembly(child_key, row["pos"], level + 1, drawings, specs,
                          warnings, seen))
        elif row["section"] == SECTION_PARTS:
            node["parts"].append(_part(row, drawings, warnings))
    return node


def _flatten(node: dict) -> list[dict]:
    out = [node]
    for child in node["children"]:
        out.extend(_flatten(child))
    return out


def assemble(vector: dict) -> dict:
    pages = vector["pages"]
    drawings, specs = _index(pages)
    warnings: list[str] = []

    referenced = {base(row["designation"])
                  for page in pages if page["kind"] == "spec"
                  for row in page["spec"]
                  if row["designation"] and base(row["designation"]) != base(
                      page["stamp"].get("designation", ""))}
    roots = [k for k in specs if k not in referenced]
    if not roots:
        raise SystemExit("Не найдена корневая сборка: все спецификации на кого-то ссылаются")
    if len(roots) > 1:
        warnings.append("В комплекте несколько корневых сборок: " + ", ".join(sorted(roots)))
    root_key = roots[0]

    seen: set[str] = set()
    root = _assembly(root_key, "", 0, drawings, specs, warnings, seen)

    orphans = [page["page"] for page in pages
               if page["kind"] == "drawing"
               and base(page["stamp"].get("designation", "")) not in seen
               and not any(base(page["stamp"].get("designation", "")) == p["designation"]
                           or base(page["stamp"].get("designation", "")) == base(p["designation"])
                           for node in _flatten(root) for p in node["parts"])]
    for number in orphans:
        stamp = pages[number - 1]["stamp"]
        warnings.append(
            f"лист {number} «{stamp.get('designation', '')} {stamp.get('title', '')}» "
            "не найден ни в одной спецификации")

    return {
        "source": vector["pdf"],
        "designation": root["designation"],
        "title": root["title"],
        "mass": root["mass"],
        "general": [],
        "assembly": root,
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Дерево комплекта из vector.json")
    ap.add_argument("vector", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("work/tz.json"))
    args = ap.parse_args()

    vector = json.loads(args.vector.read_text(encoding="utf-8"))
    tz = assemble(vector)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(tz, ensure_ascii=False, indent=1), encoding="utf-8")

    nodes = _flatten(tz["assembly"])
    parts = sum(len(n["parts"]) for n in nodes)
    print(f"{tz['designation']} {tz['title']}")
    print(f"сборок: {len(nodes)}, деталей с чертежами: {parts}, "
          f"предупреждений: {len(tz['warnings'])}")
    for line in tz["warnings"]:
        print("  ! " + line)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
