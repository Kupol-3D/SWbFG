# Локальный TLS/WebSocket ↔ UDP relay

**Status: not production.** Инкремент TASK5: локальный транспорт перед FGMS,
без match-authority, client companion и публичного deployment. Invitation
lifecycle (выдача/expiry/revocation/хранилище) и rate limits — отдельные этапы.

## Модули и границы

- `auth_gate.py`: TLS 1.3, Bearer admission, точный path `/mp`, ограничение
  сессий/кадров и redaction. Слушает **только loopback**. Старый callback
  `on_message(identity, bytes) → bytes | None` сохраняется.
- `mp_protocol.py`: чистая проверка MP-конверта/имени, без сетевого I/O.
- `relay.py`: отдельный UDP endpoint на допущенную identity,
  независимый обратный поток, ограниченные очереди и endpoint leases.

Одна бинарная WS-message переносит ровно одну датаграмму. Транспорт не
склеивает пакеты и не меняет позицию/свойства. FGMS остаётся relay движения;
его наличие не даёт общего авторитетного счёта или устойчивых игровых жизней.
Сам факт открытия UDP-сокета не доказывает доступность FGMS.

## Lifecycle gate

`on_open(identity, channel)` вызывается после auth и брони слота, до первого
чтения WS. `SessionChannel` даёт только async `send(bytes)` и `close()`;
чужую сессию или `recv` он не открывает. Производитель может отправить пакет,
даже если клиент в этот момент ничего не присылает.

`on_close(identity)` выполняется при завершении допущенной сессии. Слот
освобождается и при исключении callback. Старый channel привязан к ticket
старого допуска и не может отправлять в новую сессию той же identity.
`PolicyViolation` закрывает канал с generic 1008; callback failure — 1011,
без передачи сырого исключения клиенту или в лог. TLS/hostname проверки
не ослабляются. Токены не помещаются в URL, argv, логи или общий JSON.

## Пример подключения библиотеки

Вызывающий dev-harness предоставляет registry и TLS-контекст с сертификатом.
Пример — не готовый CLI и не автоматическая выдача приглашений:

```python
import asyncio
from gateway.auth_gate import IdentityRegistry, TlsAuthGate
from gateway.relay import MAX_PACKET_SIZE, UdpRelay

async def serve_local(registry: IdentityRegistry, server_tls):
    relay = UdpRelay(
        backend=("127.0.0.1", 5500),
        callsigns={"pilot-a": "RED001", "pilot-b": "BLUE01"},
    )
    gate = TlsAuthGate(
        registry=registry,
        on_message=relay.on_message,
        on_open=relay.on_open,
        on_close=relay.on_close,
        max_message_bytes=MAX_PACKET_SIZE,
    )
    try:
        await gate.start(host="127.0.0.1", port=8443, ssl_context=server_tls)
        await asyncio.Event().wait()
    finally:
        await gate.stop()
        await relay.close()
```

Identity в registry и ключи `callsigns` должны соответствовать. Callsign
задан серверной конфигурацией, не выбирается из первого присланного пакета.
Имена уникальны без учёта регистра; настройка копируется в immutable mapping.
Пакет другой identity/callsign отклоняется, а не переписывается.
Публичный профиль приглашения и интерфейс выдачи пока не реализованы.

## Ограничения локального инкремента

- Gate и UDP backend — loopback. Backend — фиксированный IPv4 literal и порт,
  без DNS, пользовательских destination URL, LAN/public proxy или relays.
- UDP-сокет не использует `connect()`: отправка задаёт только настроенный
  backend, приём явно сравнивает source IP/port с ним. Это проверка источника
  в приложении, не криптографическая аутентификация UDP; backend должен
  оставаться в доверенном приватном контуре.
- В интеграции gate задаётся лимит **1200 байт**. Обычный auth spike сохраняет
  свой default 4096 для совместимости прежних тестов, это не FGMP-размер.
- Header: network order, FGFS magic, protocol 1.1, MsgId **1/7**, точный MsgLen,
  ReplyPort=0, callsign 1–6 ASCII букв/цифр с NUL/нулевым padding, без `obs`.
- CHAT имеет проверяемый фиксированный размер тела 256 байт по исследованным
  исходникам. POS/property body — opaque, проверяется только наличие и bound;
  **валидный header не означает валидную native-позицию FGMS**. Полный codec,
  физическая правдоподобность и античит не реализованы.
- Обратный UDP принимается только от настроенного backend и проверяется по
  MP-конверту; в нём может быть callsign другого пилота, не только получателя.
- Очередь на активный канал ограничена; при переполнении отбрасывается самый
  старый пакет. Это политика ненадёжного потока позиций, не гарантия доставки
  чата. Writer имеет timeout; ошибки не выгружают содержимое пакета в логи.

## Reconnect и завершение

При disconnect writer/очередь отделяются, но UDP endpoint сохраняется на
конечный grace. Пакеты во время отсутствия WS отбрасываются. Быстрый reconnect
той же identity использует прежний порт, новую очередь и новый channel.

Defaults: `fgms_player_expires=10`, `expiry_margin=2`, `grace_seconds=30`.
Минимум grace — `2 * fgms_player_expires + expiry_margin`: FGMS проверяет TTL
в цикле с блокирующим select, а не удаляет запись строго на десятой секунде.
Количество leases (`max_leases`, default 8) включает и неактивные grace-порты,
и ещё не завершённые создания endpoint (pending `on_open`). Неактивный lease не
вытесняется раньше срока ради нового клиента.
Изменение server timeout/цикла требует пересмотра и реального теста политики.

Сокет закрывается после grace; `relay.close()` завершает writers/timers/UDP.
Штатный порядок — сначала `gate.stop()`, затем `relay.close()`. Гарантия
сохранения порта действует внутри одного процесса relay, не после его
перезапуска. При restart требуется дождаться очистки FGMS или подтвердить
согласованную процедуру рестарта. Игровые слоты и счёт транспорт не меняет.

## Проверки

После подготовки Python из корня проекта:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tools -p 'test_gateway*.py' -v
```

Auth/lifecycle тесты используют настоящие loopback TLS+WS, временные CA и
проверку hostname. Relay transport-тесты используют реальные TLS+WS+UDP,
но backend — **FAKE**, а позиционные body — явно ненативные фикстуры.
Отдельный детерминированный тест backpressure может использовать тестовый
channel; это не замер TCP congestion или задержек FlightGear.

## Настоящий FGMS: отдельная приёмка

На машине с Docker, из корня проекта:

```sh
docker compose -p space-arena -f server/space/compose.json -f server/space/compose.local.json config
docker compose -p space-arena -f server/space/compose.json -f server/space/compose.local.json build
docker compose -p space-arena -f server/space/compose.json -f server/space/compose.local.json up -d
```

Backend harness указывает `127.0.0.1:5500`, затем проверяются **настоящие**
MP-данные и двусторонняя доставка/регистрация/expiry, а не повтор fake-suite.
Создать native-позицию угадыванием C++ sizeof нельзя; нужны проверенные
реальные пакеты/клиенты. Client bridge ещё не реализован TASK5, поэтому
полный маршрут двух fgfs — следующий интеграционный рубеж.

Наличие запущенного Docker рядом с fake-suite не превращает её в FGMS-тест.
Если FGMS/FG не запускались, в PR явно отметить runtime-blocked/not-tested.
Подробный MP-контракт — `docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md`.
