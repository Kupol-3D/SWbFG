# TASK2+ — отчёт о выполнении: три модели TIE и унификация со стилем TASK2

**Статус: Blender MVP + статическая интеграция пакетов FlightGear завершены; полная внутриигровая приёмка НЕ завершена.** Суффикс `COMPLETED` у имени файла означает выполнение ограниченного объёма поставки и **не скрывает** открытых блокеров (runtime, визуальная приёмка, столкновения, FPS, лицензии/публикация). Дата отчёта: **14 ноября 2026**.

## Цель

Подготовить три корабля TIE (Royal Guard, M1 Bizarro, Boarding Craft) как игровые ассеты/экспорты и статические пакеты FlightGear в едином стиле с TASK2 (`sa_tie`: оси, воспроизводимые артефакты, ALS-совместимый visual path, явные границы null/FDM). Это поставка Blender-ассетов и статической интеграции, **не** готовый пилотируемый боевой продукт.

## Артефакты (относительные пути)

### Пакет ассетов (источник подготовки)
- Корень: `work/TIE_Game_Asset_Pack_v1/`
- Примечание: `TIE_Game_Asset_Pack_v1` — внешний входной пакет (референсы/STL/контракты); в репозиторий не входит и тестами не требуется.
- На корабль: `ships/<id>/working/02_game.blend`, `ships/<id>/export/02_game.blend`, `ships/<id>/export/{game.glb,lod1.glb,lod2.glb,collision.glb,game_lod0.ac,game_lod1.ac,game_lod2.ac,cockpit.ac,integration.json,metrics.json,textures/*,ac_scenes/*}`, `ships/<id>/export/previews/02_game/*.png`, `ships/<id>/export/previews/lods/*.png`
- Сводки/аудит: `work/TIE_Game_Asset_Pack_v1/working/delivery_summary.json`, `working/visual_review.json`, `working/material_preview_report.json`, `working/audit/FINAL_ACCEPTANCE.md`, `working/audit/contact_*.png`

### Сгенерированные пакеты FlightGear (Bombable)
- `Aircraft/sa_tie_royal_guard/` (set/model/effects/config/assisted/sources/licence/readme)
- `Aircraft/sa_tie_m1/`
- `Aircraft/sa_tie_boarding/`
- Интеграционный отчёт: `docs/TIE_INTEGRATION_REPORT.md`

## Масштаб, оси, ориентация (из источника, подтверждённые измерения)

- Единицы: **метры**. Кадр ассета: **+X вправо, +Y вперёд, +Z вверх**. Исходная ориентация STL запечена с поворотом 180° по Z (raw нос −Y, право −X, верх +Z), масштаб равномерный (`scale_and_axes.json` / integration). Источник (STL, README/LICENSE, профили) сохранён.
- SHA-256 STL (не изменялись): Royal Guard `353eca3b0e663f1d5459e42d12815ab9d3e8a57d7e49bd057f02d2d84260db13`, M1 `7c423cc3bcce5bc4aa9549f9f7500414e9f24c83d014277c5520fd9347862788`, Boarding `18f934f7e8eaac0abff20a846baeb2305accbcc12c62224fd5ca54905db520c7`.
- Измеренные размеры W/L/H (м), по текущим integration/metrics:
  - **Royal Guard:** 10.2375 / 9.6 / 6.1793; LOD0/1/2 (metrics) 52,249 / 23,752 / 7,601 трис.
  - **M1 Bizarro:** 7.3851 / 6.3 / 6.9769 (расхождения W/H с карточкой U1 сохранены — пропорции не искажались); LOD0/1/2 42,748 / 19,001 / 6,650.
  - **Boarding Craft:** 9.2757 / 7.6 / 4.8567 (конфликты карточных размеров зафиксированы, пропорции сохранены); LOD0/1/2 66,496 / 28,499 / 9,502; cockpit eye MVP `[1.8, 3.5416, -0.05]` (outboard, приближённо).
- AC/FG преобразование (в пакетах): точка ассета `(X,Y,Z)` → модель FG `(-Y, X, Z)`, смещения вида `(right, up, aft) = (X, Z, -Y)`; камеры берутся из `integration.json`, не угадываются.

## Кокпиты и ограничения геометрии

- **Royal Guard:** канонический центр, `PilotCamera`.
- **M1 Bizarro:** remote, **NON_CANON_Gameplay interior** (`RemoteOperatorCamera`), явно помечен как неканоническая игровая адаптация.
- **Boarding Craft:** **approximated outboard MVP**, не авторский полый/канонический интерьер; правая сторона модуля, собственные props, поверхность корпуса после intended decimation не вырезалась как «настоящая кабина».
- Источники — STL авторской печати/моделирования: остаются non-manifold/шероховатости/артефакты нормалей; очистка — только дубликаты/вырожденные/нулевые грани, без агрессивного weld и без реконструкции крупных поверхностей. Это ограничения источника, не «новые корабли».

## Материалы/UV

- Простые узнаваемые материалы и собственные процедурные grid-текстуры (без reference collage, без заявлений о запечённом сложном физически корректном PBR). UV/текстуры по экспортным данным проверялись на целостность ссылок; детализация — в metrics/integration.

## Тесты и воспроизводимость (фактические прогоны)

- Ранее Beam: **69 тестов OK** (статический/пакетный объём, включая delivery-проверки по факту наличия артефактов). Актуальный повторный прогон в этом аудите: см. `working/audit/FINAL_ACCEPTANCE.md` / итог ниже (сумма по фактическим счётчикам runner, без завышения).
- **69 → 79 corroborated (2026-11-15, docs-recorded re-run):** `python3 -m unittest tools.test_tie_ac_export tools.test_build_assisted_fdm tools.test_build_tie_packages tools.test_tie_delivery tools.test_sa_tie` → **Ran 79, OK**; целевой `tools.test_tie_delivery` → Ran 4, OK. Прежние тест-факты не переписаны; рост счётчика — итог добавленных в ходе TASK2+ модулей, не пересмотр прежних результатов.
- Команды воспроизведения (из корня репозитория, при наличии входов):
  - `python3 tools/build_tie_packages.py --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships --output-root "$PWD/Aircraft"`
  - `python3 tools/build_tie_packages.py --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships --output-root "$PWD/Aircraft" --check`
  - `python3 Aircraft/sa_tie/Sources/build_tie.py --check`
  - `python3 -m unittest tools.test_tie_ac_export tools.test_build_assisted_fdm tools.test_build_tie_packages tools.test_tie_delivery tools.test_sa_tie -v` (и др. целевые модули по необходимости)

## FlightGear / runtime (не принято)

- Успешного скриншота **нет**. Хост: **FlightGear 2024.1.4** (SimGear 2024.1.4, OSG 3.6.5) ≠ закреплённый проектом **2024.1.7** → runtime-приёмка не заявляется.
- Две ограниченные попытки (Royal Guard): null-модель дошла до инициализации terrain без ошибок загрузки aircraft/model в логе; попытка с HTTP/screenshot не дала снимка (endpoint недоступен в окне ожидания). Логи: `work/runtime/tie-flightgear/royal_guard_null/fgfs.log`, `work/runtime/tie-flightgear/royal_guard_http/fgfs.log` (пути workspace-relative; проверено существование). M1/Boarding в игре не запускались, FPS не измерялся.
- Пакеты по умолчанию `<flight-model>null</flight-model>`. Опциональные assisted FDM-файлы — отдельные, подключаются осознанно и не комбинируются с «обычной» аэродинамикой; не приняты runtime.

## Визуальная приёмка (обновление 2026-11-14, фактическое чтение оркестратором)

- **Статическая Blender-визуальная приёмка ВЫПОЛНЕНА оркестратором с реальной vision-проверкой:** оркестратор лично прочитал инструментом `read` три контакт-листа `work/TIE_Game_Asset_Pack_v1/working/audit/contact_royal_guard.png`, `contact_m1_bizarro.png`, `contact_boarding_craft.png`, собранных из текущих финальных оригиналов (top/front/right/isometric/cockpit + LOD1/LOD2 isometric, EEVEE). Это фактическое наблюдение модели, а не self-claim агента.
- Наблюдено: основные силуэты 3 кораблей узнаваемы; Royal crimson+dark panels читаются; M1 — неравные два модуля (gun/control), source identity сохранена; Boarding cockpit — собственные outboard рамка/панель и чистый обзор наружу, без гигантских вырезов; LOD1/2 — широкие силуэты сохранены у всех трёх.
- Все 7 видов × 3 корабля зафиксированы `VISION_REVIEWED_BY_ORCHESTRATOR` в `working/visual_review.json`, scope **BlenderMVP_known_limitations**; сводка: `working/audit/VISUAL_ACCEPTED.md`.
- **Различие ролей проверок сохранено:** независимая Iceman-сессия (`working/audit/iceman_stage2_visual_review.md`) vision НЕ имела (вердикты UNVERIFIED) и остаётся отдельной записью без претензии на визуальную приёмку; приёмка оркестратора её не переписывает.
- Ограничения в силе: исходные print/roughness/normal-дефекты и non-manifold топология сохранены (не «фотореализм», не «исправленная топология»); Boarding cockpit — outboard-приближение MVP, не авторская полая кабина; M1 интерьер — remote NON_CANON_Gameplay. Игра/FG/FPS/коллизии/runtime-LOD этой проверкой **не приняты**.

## Не заявляется

- Принятая физическая динамика, соответствие лорным MGLT/G/ускорениям в SI, боевые/сетевые/столкновительные системы, производительность, финальная **внутриигровая** визуальная приёмка (статическая Blender-приёмка оркестратором выполнена 2026-11-14, см. секцию выше; in-game скриншотов нет; человек/модель — только где явно подтверждено, Boarding MVP ограничен).
- Коллизии и runtime LOD — **подготовленные данные, интеграция не выполнена**. Лорные константы не переводились «молча».

## Лицензии

- Thingiverse/STL: CC BY, **точная версия не подтверждена** в поставленных файлах; для Boarding remix-цепочка требует до-проверки. Права на вселенную/IP отдельно не подтверждены. Публикация не выполнялась.

## Corrective appendix (2026-11-15, Beam-owned 3D repair)

Status: corrective repair pass applied to owned TIE scripts/exports; in-game FG acceptance still NOT claimed.

### Root causes addressed
- Crude rectangular face-deletion aperture replaced with bounded octagonal aperture on GAME copies only; cockpit interiors rebuilt with lining, floor, seat, panel, indicators, spokes, frame ring and transparent glass pane.
- Royal Guard keeps central spherical module and PilotCamera; M1 Bizarro keeps remote left control module with NON_CANON_Gameplay interior; Boarding Craft camera moved from outboard fallback to inside the right module behind the author window.
- LOD0 budgets raised so shells are not destroyed by aggressive decimation; normal consistency pass added; solar UV changed to planar YZ with bounded [0..1] report.
- AC export materials now preserve emission (strength scaled into emis), specular and alpha/transparency flags.
- Export blends synced to export/02_game.blend; packages rebuilt and --check passed.

### Artifacts updated
- ships/*/working/02_game.blend and ships/*/export/02_game.blend
- ships/*/export/previews/02_game/*.png and lods/*.png
- ships/*/export/integration.json, metrics.json, ac_scenes/*
- Aircraft/sa_tie_royal_guard, Aircraft/sa_tie_m1, Aircraft/sa_tie_boarding rebuilt.

### Remaining blockers
- FlightGear runtime capture not performed in this pass; host FG 2024.1.4 vs pinned 2024.1.7.
- Collision backend still pending.
- Visual acceptance by orchestrator image check required.
