# TLS/auth gate — локальный transport spike

**Status: not production.** Только локальный экспериментальный компонент.

`auth_gate.py` — библиотека допуска к бинарному WebSocket-каналу.
Это **не полный gateway и не production endpoint**: UDP backend, client
bridge, CLI deployment, управление приглашениями (invitation lifecycle:
срок/отзыв) и rate limits ещё не реализованы.

## Реализованный контракт

- Только loopback (`127.0.0.1`/`::1`), обязательный TLS context с минимум
  TLS 1.3; без контекста или с TLS 1.2 сервер не стартует.
- Точный path `/mp`, Bearer auth в заголовке, без токена в query URL.
- Immutable registry identity → SHA256 токена; `generate_token()` генерирует
  секрет для вызывающего кода, а не общую конфигурацию репозитория.
- Один активный канал на identity, общий `max_sessions`, освобождение слота
  в `finally`. Это слот транспорта, **не** бюджет кораблей матча.
- Binary frames до 4096 байт в этом spike; текст/oversize отклоняются.
  Для будущего FGMP relay необходим более строгий предел 1200 байт.
- Async callback `on_message(identity, bytes) → bytes | None`; сейчас
  обратный ответ связан с входным сообщением. Для unsolicited UDP требуется
  расширение lifecycle/отправки, поставленное в TASK5.
- Redaction логов заголовков/токенов/данных кадров.

## Разработка и тесты

Из корня проекта после создания/активации `.venv`:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tools -p 'test_gateway_auth.py'
```

Тесты создают временные CA/сертификат и реальные локальные TLS/WebSocket-
соединения. Клиент проверяет CA/hostname; отрицательные сценарии включают
неверный сертификат, токен, duplicate identity, лимиты и очистку слота.
Тестовый callback — echo, не настоящий FGMS.

Runtime dependency — `websockets` из `requirements.txt`; `cryptography`
из `requirements-test.txt` нужна только для временных сертификатов тестов.
Не сохранять сгенерированные сертификаты/секреты в исходники.

Архитектура: `docs/SPACE_ARENA_GATEWAY.md`; точный будущий MP-контракт:
`docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md`. Python-тесты не являются проверкой
FlightGear/FGMS, публичного endpoint или игрового матча.
