# TASK5 — отчёт о локальном TLS/WebSocket ↔ UDP relay

**Статус: локальный транспортный инкремент выполнен и протестирован.
Настоящий FGMS smoke не проведён.** Требования: [TASK5](../TASK/TASK5.md).

## Сделано

- `gateway/relay.py`: один UDP endpoint на identity; одна бинарная WS-message
  соответствует одной датаграмме; обратный поток не требует отправки клиента.
- Gate lifecycle `on_open/on_close`, ограниченные очереди, timeout writer,
  cleanup, запрет duplicate identity и независимые сессии.
- `mp_protocol.py`: magic/version/MsgId allowlist/точный MsgLen/ReplyPort,
  лимит 1200 байт, callsign привязан к authenticated identity.
- Фиксированный loopback backend, проверка источника UDP, без произвольного
  прокси. TLS 1.3 и hostname verification не ослаблены; секреты не логируются.
- Bounded lease/grace: быстрый reconnect сохраняет порт, после grace endpoint
  освобождается; учитываются `playerexpires`, sweep margin и pending leases.
  Транспорт не выдаёт жизнь и не меняет match-state.

## Проверки

```sh
python3 -m unittest discover -s tools -p 'test_gateway*.py'
```

114 тестов: auth, lifecycle, протокол и реальные loopback TLS/WS/UDP сокеты.
Покрыты два клиента, unsolicited UDP, malformed/spoofed/oversize frames,
duplicate identity, reconnect, bounded queues, cleanup, TLS/hostname и
redaction. Backend явно **fake**, позиционные тела не являются native FGMP.
Зависимости закреплены в `gateway/requirements*.txt`.

## Ограничения и продолжение

FGMS/Docker и FlightGear в текущем аудите не запускались. Инструкция
[gateway README](../gateway/README.md) содержит отдельный настоящий smoke:
нужны native-пакеты, двусторонняя доставка, регистрация и expiry FGMS.
Port lease гарантирован только внутри процесса relay, не после рестарта.
Проверка envelope не является полным codec позиций или античитом.

Client bridge, публичный deploy, invite expiry/revocation/rate limits и
авторитетный матч — следующие инкременты. Это не production-ready шлюз
и не доказательство соединения двух настоящих FlightGear.
