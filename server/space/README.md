# Приватный FGMS-контур

Compose-project `space-arena`, image `space-arena-fgms:0.13.10`.
`../Dockerfile` закрепляет ревизию FGMS. Default `compose.json` не
публикует host-порты: сеть `arena` имеет `internal: true`,
`expose: 5000/udp` — метаданные, не разрешение публичного доступа.

**Статус:** deployment scaffolding, а не готовый интернет-сервер.
TLS/auth spike находится в `gateway/`, но полного relay/invites ещё нет.
Сетевая работоспособность Docker-контура требует реального smoke-теста;
структурные Python-тесты её не подтверждают.

## Команды администратора

Из этого каталога:

```sh
docker compose -p space-arena -f compose.json config
docker compose -p space-arena -f compose.json build
docker compose -p space-arena -f compose.json up -d
docker compose -p space-arena -f compose.json ps
docker compose -p space-arena -f compose.json exec fgms tail -n 100 -f /tmp/fgms.log
docker compose -p space-arena -f compose.json down --timeout 10
```

Лог на tmpfs исчезает при остановке: результаты приёмки копировать отдельно
и не добавлять необработанные runtime-логи в Git. Bind `0.0.0.0` внутри
контейнера не равен публикации интерфейса хоста.

## Локальный smoke, только loopback

`compose.local.json` — **общий безопасный пример**, намеренно включён в Git.
Он добавляет единственную публикацию `127.0.0.1:5500 → 5000/udp` и сеть
`smoke`; приватная `arena` сохраняется. Никогда не заменять IP на публичный
для обхода незавершённого gateway.

```sh
docker compose -p space-arena -f compose.json -f compose.local.json config
docker compose -p space-arena -f compose.json -f compose.local.json up -d
docker compose -p space-arena -f compose.json -f compose.local.json down --timeout 10
```

Диагностический клиент (из корня репозитория, пока только dry-run):

```sh
python tools/launch.py --mode baseline --network --server 127.0.0.1 \
  --server-port 5500 --local-ip 127.0.0.1 --callsign PIL001 --dry-run
```

Для реального теста убрать `--dry-run` при установленном FG. Нужны два
клиента с разными callsign/receive-port и наблюдение движения в обе стороны.
Это локальный UDP smoke, **не** защищённое интернет-подключение.

Cleanup — только свой project: явно задавать `-p space-arena` и те же `-f`.
Никакого `docker prune`, массового удаления контейнеров, изменения firewall
или настройки VPS эти команды не выполняют.
