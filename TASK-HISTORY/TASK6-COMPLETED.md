# TASK6 — отчёт о выполнении: код реализован, host acceptance pending

Исходные требования: [TASK6](../TASK/TASK6.md).

## Результат задачи

Модуль `client/space_ui/` предоставляет Tk/ttk форму профиля, фракции и
демонстрационного каталога с доступными/заблокированными кораблями. MockAdapter
отделён от будущей сети; RelayStub сообщает unavailable, а не успешный handshake.
SoloSession запускает именно штатный c172p через argv без shell и секретов;
выбор демонстрационного X-wing не подменяется c172p. Ошибки профиля/пути и
повторное нажатие проверяются headless-тестами. Слоты и server-ready UI себе
не назначает; preparing/connecting/ready/error не означают рабочий VPS relay.

**Дата аудита/правки:** 2026-11-11. **Границы аудита:** локальные правки в
`client/space_ui/**`, `tools/test_space_ui.py`, `client/space_ui/README.md`.

## Статус

- Код внешнего экрана (Tk/ttk форма + headless core + mock-адаптер + solo
  c172p-запуск через argv) — **реализован** и покрыт офлайн-тестами.
- **Host acceptance — НЕ выполнено**: в этой среде нет Tcl/Tk, display и
  FlightGear. Реальный FG/c172p из кнопки, скрин формы, клики и визуальная
  приёмка, а также Windows/Linux-прогоны — **not tested** (см. MANUAL_TEST.md).
  Никакие GUI/FG/visual проверки не заявляются пройденными.

## Выполнено в этой сессии (аудит TASK6/TASK7)

1. **Исправлен дефект:** неиспользуемый импорт `DEMO_CATALOG` удалён из
   `client/space_ui/view.py` (импортировался, но в модуле не использовался).
2. **Регрессия:** в `tools/test_space_ui.py` добавлены `StaticHygiene` —
   AST-скан всех модулей `client/space_ui/` на неиспользуемые импорты и
   прямой контроль, что импорт `DEMO_CATALOG` в `view.py` не вернётся
   (линтера в офлайн-наборе нет, поэтому страж — тест).
3. **Проверено утверждение README про .gitignore:** правило `*.local.json`
   (строка 22 `.gitignore`) покрывает `space-ui.local.json` на любой глубине;
   исключение (`!`) только для `server/space/compose.local.json`. Утверждение
   README **верно**; в `client/space_ui/README.md` явно указан паттерн
   `*.local.json`, чтобы вопрос не возникал снова. Git-команды не запускались;
   сопоставление проверено моделью gitignore-семантики на Python.
4. Прочих локальных дефектов в границах TASK6 не найдено: profile-whitelist,
   endpoint-policy, bounded read, FIFO, argv-hygiene solo-запуска, lifecycle
   одного дочернего процесса и границы импорта (core без tkinter) проверены
   тестами и дополнительными зондами (symlink, относительный путь, trailing-dot
   hostname, non-ASCII host, границы порта).

## Файлы

- `client/space_ui/view.py` — удалён неиспользуемый импорт.
- `client/space_ui/README.md` — уточнён паттерн исключения профиля.
- `tools/test_space_ui.py` — `import ast`, классы `StaticHygiene`
  (2 теста).

## Проверки (фактические прогоны)

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/pylibs python3 -m unittest
  tools.test_space_ui tools.test_match_core tools.test_match_snapshots` →
  **Ran 142 tests, OK** (Python 3.11.2).
- Документированные команды README работают: `python -m unittest discover -s
  tools -p 'test_space_ui.py'` и `-p 'test_match*.py'`.
- Страж regression доказан на мутации: возврат `DEMO_CATALOG` в импорт
  детектируется (проверено на копии файла вне репозитория).

## Не выполнено / следующие шаги

- Хостовая приёмка по `client/space_ui/MANUAL_TEST.md` на машине с Tk+display:
  `python -m tkinter`, `python -m client.space_ui.smoke` (exit 0), форма,
  клики, реальный запуск c172p solo, скриншоты. Windows и Linux — отдельными
  строками tested/not tested. Целевой Python 3.14 — проверить Tk под эту сборку.
- Relay-адаптер, VPS, матч, installer — вне TASK6 (зафиксировано в README).
