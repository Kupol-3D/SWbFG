# Защищённый транспорт

```text
FlightGear ↔ local loopback UDP companion ↔ TLS WebSocket gateway ↔ private FGMS
```

Публичен только защищённый endpoint. Native UDP FGMS не публикуется в
интернет; SSH — администрирование, не игровой UDP-туннель. Игроку не нужны
SSH-ключ/VPN/ручная настройка своего сетевого адреса.

## Статус

`gateway/auth_gate.py` реализует только локальный TLS/auth spike. UDP relay,
client companion, invitation lifecycle и deployment требуют реализации/
приёмки. Scaffolding `server/space` сам по себе не готовый сервер.

WebSocket over TLS — первый кандидат: одна binary-message = одна MP-
датаграмма. Стандартная проверка CA/hostname обязательна; без `verify=false`,
самодельного crypto и токена в query URL. TCP head-of-line задержки при
потерях измеряются в игровом сценарии; datagram latency не обещается заранее.

## Доступ и ресурсы

- Приглашения выдаёт администратор; identity отлична от callsign/соединения.
- Секреты не попадают в Git, общие JSON, URL, argv, логи и дистрибутив.
- Backend UDP создаётся после допуска; адрес фиксирован серверной
  конфигурацией, не принимается произвольно от клиента.
- Ограничить handshake/частоту/сессии/очередь/размер/срок жизни.
  Expiry/revocation/хранилище пока не реализованы.
- Duplicate identity/reconnect имеют bounded policy. Освобождение слота
  транспорта не возвращает корабль в match budget.

## Клиент и матч

Профиль хранит локальный путь FG/endpoint/callsign. «Подключиться» готовит
companion, проверяет канал и запускает FG на loopback tx/rx. Ошибка канала —
ошибка UI, не выдуманный status connected. `wss://` нельзя подставлять как
обычный адрес в `--multiplay=out`.

Диагностический `tools/launch.py` пока поддерживает solo/direct UDP, а не
этот маршрут. Auth не проверяет истинность HP/kill claims; match-authority —
отдельный компонент с собственной приёмкой.

## Перед публичным deployment

Изоляция FGMS/admin/tracker, отказ неавторизованным/просроченным/отозванным
приглашениям, лимиты/cleanup, TLS negative tests, двусторонний прогон двух FG,
reconnect без лишних сессий/жизней, сборка клиента на целевых ОС обязательны.
Не публиковать FGMS UDP ради обхода незавершённого gateway.
