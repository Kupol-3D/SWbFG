# TASK5 — Двунаправленный TLS/WebSocket ↔ FGMS UDP relay

**Срок:** 2–3 рабочих дня. Ветка `feat/task5-relay`.
Вход: `gateway/auth_gate.py`, его тесты, `gateway/requirements-test.txt`,
`docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md`, `server/space/`.

## Цель
Превратить проверенный TLS/auth spike в маленький локальный транспортный
инкремент. FGMS остаётся relay движения, не судья урона или счёта.

## Выход
- `gateway/relay.py`, `tools/test_gateway_relay.py`, README gateway.
  Изменения `auth_gate.py` минимальны: нынешний callback «вход → один ответ»
  недостаточен для независимых UDP-сообщений и событий connect/disconnect.
- Одна binary WS-message = одна UDP-датаграмма. Асинхронный обратный поток
  работает, даже когда клиент ничего не присылает; bounded queues/cleanup.
- Валидация полного MP-заголовка/MsgLen/allowlist MsgId, лимит 1200 байт,
  callsign привязан к authenticated identity. Backend адрес фиксирован,
  отдельный UDP endpoint на identity; не строить произвольный UDP-прокси.
- Явная bounded lease/grace политика reconnect с учётом `playerexpires`:
  либо сохраняем endpoint, либо ждём expiry. Не терять порт случайно при
  мгновенном reconnect; не выдавать новую жизнь и не менять match-state.

## Готово, когда
Реальные loopback TLS+WS+UDP тесты: два клиента, вход/ответ и unsolicited
пакет от UDP backend; rejection невалидных/лишних/подменённых пакетов,
duplicate identity, reconnect и освобождение ресурсов. Предыдущие тесты
зелёные, токенов/пакетов в логах нет. UDP fake-backend назван именно fake.
Если доступен настоящий FGMS — отдельный smoke и отчёт, иначе приложить
команду и runtime-blocker. Это НЕ проверка двух реальных FlightGear.

## Границы
Gate остаётся loopback-only, TLS/hostname verification не ослаблять.
Без публичного deploy, launcher, нового crypto, match/damage сервера.
Rate limits/invite expiry/revocation и local client bridge — следующие
инкременты, не объявлять этот шаг production-ready.
