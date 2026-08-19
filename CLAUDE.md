# CLAUDE.md — Карты обмера

Два документа из чертежей:

- **карта обмера** — по одному листу, система из трёх агентов:
  чертёж → размеченный JPG + карта обмера в Word. Многостраничный файл
  сначала разбирается на листы (`.claude/rules/komplekt.md`);
- **требования по изготовлению (ТЗ)** — по всему комплекту:
  векторный PDF (сборка + спецификации + детали) → docx.
  Правила — `.claude/rules/tz-format.md`, запуск — `python -m app.tz_pipeline`.

## Главный принцип

**Агенты не считают пиксели — считает код.** Координаты, поиск свободного места
под номер, кропы, генерация docx — всё в `tools/`. Агенты читают надписи
и принимают решения: что это за размер, к какому виду относится, какой номер дать.

Никогда не заменяй вызов инструмента прикидкой координат на глаз.

## Роли

| Агент | Файл | Вход → выход |
|-------|------|--------------|
| Разметчик | `.claude/agents/markup-agent.md` | чертёж → `work/markup.json` + JPG с номерами |
| Картограф | `.claude/agents/docx-agent.md` | `markup.json` → docx |
| Контролёр | `.claude/agents/review-agent.md` | всё вместе → `output/review.md` |

Общий сценарий — `.claude/skills/karta-obmera/SKILL.md`.

## Правила предметной области

Читать перед любой работой с картами, они важнее общих соображений:

- `.claude/rules/sheet-zones.md` — зоны листа: чертёж, штамп, спецификация,
  текстовая часть; что из них не нумеруется никогда
- `.claude/rules/komplekt.md` — многостраничный файл: разбор на листы,
  какие листы идут в работу, почему считать без спроса нельзя
- `.claude/rules/numbering.md` — порядок групп, порядок позиций, подномера
- `.claude/rules/glossary.md` — спецсимволы, сокращения, допуски
- `.claude/rules/tt-rules.md` — технические требования
- `.claude/rules/markup-format.md` — контракт `work/markup.json`
- `.claude/rules/tz-format.md` — структура ТЗ, контракт `work/tz.json`, канон ТТ

## Соглашения

- Интерпретатор — `.venv/Scripts/python.exe`, не системный python
- Пути с кириллицей: читать/писать изображения через `np.fromfile` + `cv2.imdecode`
  и `cv2.imencode` + `Path.write_bytes`. `cv2.imread` на таких путях молча вернёт `None`
- Язык всего пользовательского текста, комментариев и сообщений — русский
- Комментарии в коде — только там, где неочевидно **почему**
- Рамки допусков формы и расположения никогда не расшифровываются текстом,
  только кропом с чертежа
- Колонки «Фактические значения» в карте всегда пустые

## Проверка изменений

```powershell
.venv\Scripts\python.exe -m app.tz_pipeline "input\Барабан натяжной МШЕФ 02.6.02.07.00.000 СБ.pdf"
.venv\Scripts\python.exe tools\compare_tz.py --tz "output\...(ТЗ).docx" --gold "tests\gold\tz\МШЕФ 02.6.02.07.00.000\tz.docx"
```

```powershell
.venv\Scripts\python.exe tools\detect_text.py "tests\gold\5489.0123.0000.28\drawing.jpg" -o work\blocks.json --debug work\blocks.png
.venv\Scripts\python.exe tools\detect_frames.py "tests\gold\5489.0123.0000.28\drawing.jpg" -o work\frames.json
.venv\Scripts\python.exe tools\check_markup.py work\markup.json
.venv\Scripts\python.exe tools\compare_gold.py --card "output\...docx" --gold "tests\gold\5489.0123.0000.28\card.docx" --quiet
```

Ориентиры на эталоне `5489.0123.0000.28`: ~255 текстовых блоков, ровно 4 рамки
допусков, 61 позиция в карте.

## Приложение

```powershell
.venv\Scripts\python.exe -m app        # http://127.0.0.1:8765
```

| Файл | Роль |
|------|------|
| `app/server.py` | FastAPI: загрузка, статус задания, скачивание результата |
| `app/jobs.py` | задание на диске `work/jobs/<id>/`, переживает перезапуск |
| `app/runner.py` | стадии обоих режимов поверх `tools/` |
| `app/agents/reader.py` | чтение контактных листов и разметка через Qwen |
| `app/agents/tz_writer.py` | «Общие данные» и канон формулировок |
| `app/web/` | интерфейс без сборки; плотный служебный стиль, правится в `style.css` |

Промпты — `app/prompts/*.md`, правятся текстом наравне с правилами.
Ключ OpenRouter — только в `.env`. **В `.env.example` ключ класть нельзя**:
файл отслеживается git.

## Что дальше (отдельной задачей)

Редактор разметки в приложении: подвинуть номер мышкой, поправить значение,
объединить позиции. Делать после того, как чтение стабильно отработает
на реальных заказах.
