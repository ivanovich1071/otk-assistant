---
name: karta-word
description: Собрать карту обмера в Word из готовой разметки work/markup.json. Использовать при просьбах «перенеси в ворд», «сделай карту обмера из разметки», «пересобери docx».
---

# Карта обмера в Word

Запускает Картографа поверх уже готовой разметки — без повторного чтения чертежа.

## Порядок

1. Убедись, что `work/markup.json` существует и проходит проверку:

   ```
   .venv/Scripts/python.exe tools/check_markup.py work/markup.json
   ```

   Если файла нет — сначала нужна разметка (скилл `razmetka`).

2. Запусти агента `docx-agent`:

   > Собери карту обмера из `work/markup.json` по `.claude/agents/docx-agent.md`.
   > Чертёж: путь из поля `drawing`. Результат: `output/<имя> (карта обмера).docx`.

3. Покажи результат:

   ```
   .venv/Scripts/python.exe tools/compare_gold.py --card "output/<имя> (карта обмера).docx"
   ```

   и отдай файл пользователю через `SendUserFile`.

## Если есть эталон

Когда для этой детали в `tests/gold/` лежит эталонная карта, сравни:

```
.venv/Scripts/python.exe tools/compare_gold.py --card "output/<имя> (карта обмера).docx" --gold "tests/gold/<обозначение>/card.docx"
```

и покажи проценты совпадения позиций и значений.
