# Offline-ядро матча — TASK7

Чистая Python-библиотека правил, отдельно от FGMS/gateway/клиентского UI.
Никаких сокетов, собственного сервера урона, чтения часов, random или
файлового storage. FlightGear и дополнительные зависимости для ядра не нужны.

## API

```python
from match_core import (
    MatchError, Transition, create_match, apply_event,
    snapshot, restore_snapshot,
)
```

- `create_match(match_id, catalog, *, target_kills=15,
  exhaustion_enabled=False, time_limit_seconds=None)` — новый lobby.
- `apply_event(state, event)` — `Transition(state, accepted, reason)`.
  Входы не меняются; rejected event не расходует слот и не пишет ledger.
- `snapshot(state)` — проверенный независимый JSON DTO v1.
- `restore_snapshot(payload)` — восстановленное проверенное состояние.
- `MatchError` — некорректная конфигурация/состояние/snapshot;
  обычный отказ игровому событию возвращается через `accepted=False`.

В `catalog` перечисляются конкретные типы для `rebels`/`empire`. Это данные
правил, а не автоматическое обнаружение установленных моделей FG.
Полная схема событий и trust boundary — [CONTRACT.md](CONTRACT.md).

## Короткий пример без FG и сети

Из корня проекта, в Python:

```python
import json
from match_core import create_match, apply_event, snapshot, restore_snapshot

state = create_match(
    "round-1",
    {"rebels": ["xwing", "ywing"], "empire": ["tie", "bomber"]},
    exhaustion_enabled=True,  # явно включён для этого маленького примера
)
events = [
    ("join-a", "join", {"player_id": "pilot-a", "team_id": "rebels", "callsign": "RED001"}),
    ("join-b", "join", {"player_id": "pilot-b", "team_id": "empire", "callsign": "BLUE01"}),
    ("start", "start", {}),
    ("spawn-a", "spawn", {"player_id": "pilot-a", "ship_id": "xwing", "spawn_id": "a-1"}),
    ("spawn-b", "spawn", {"player_id": "pilot-b", "ship_id": "tie", "spawn_id": "b-1"}),
    ("death-b", "death", {"victim_spawn_id": "b-1", "killer_spawn_id": "a-1"}),
]
for event_id, kind, fields in events:
    event = {"version": 1, "match_id": "round-1", "event_id": event_id,
             "type": kind, **fields}
    result = apply_event(state, event)
    assert result.accepted, result.reason
    state = result.state

assert state["scores"] == {"rebels": 1, "empire": 0}
assert state["players"]["pilot-b"]["used_ship_ids"] == ["tie"]
saved = json.loads(json.dumps(snapshot(state), sort_keys=True))
restored = restore_snapshot(saved)
assert restored == state
```

У Empire остаётся другой тип — `bomber`; повторно вылететь на `tie` нельзя.
Цель всё ещё 15, в примере она не снижалась. В настоящем маленьком матче
нужно заранее выбрать fallback, иначе 4×4×3 не позволяют получить 15 потерь.

## События и ограничения

Join фиксирует команду, start фиксирует состав. Reconnect меняет только
callsign существующего player ID, не потраченные типы. Spawn допускается
только один активный и один раз на конкретном типе; spawn ID не рециклируется.
Death закрывает живой текущий victim spawn и даёт одно очко врагу, crash
не даёт очков. Friendly/self/stale/replayed события отвергаются.

В v1 killer spawn тоже должен быть живым: posthumous kill не поддерживается.
HP/попадания/подтверждение реальной смерти, reserve/rollback неудачного старта
FG, disconnect/abandon и batch одновременных смертей — отдельные задачи.
Это не скрытые дополнительные функции текущего reducer.

Таймер задаётся событием `tick` с целым `elapsed_seconds`; библиотека не
смотрит на wall-clock. Tick подаёт авторитетный таймер, не UI игрока.
Частоту ticks и объём accepted history контролирует интегратор: это журнал
матчевых событий, не поток кадров или выстрелов с частотой рендера.

## Snapshot — не доказательство подлинности

Сохранённое состояние проверяется вместе с историей принятых событий.
Несогласованность бюджета/спавнов/счёта/ledger отвергается, не исправляется
молча. JSON storage/подпись/атомарный commit реализует вызывающий компонент.
Нельзя принимать snapshot от клиента как источник истины: согласованно
подделанная история без внешней аутентификации не становится достоверной.

Обычный `apply_event` получает последнее состояние от create/restore/предыдущего
принятого перехода. Произвольно отредактированный словарь не является валидным
состоянием. Для внешней границы восстановления использовать `restore_snapshot`.
Параллельные вызовы на старом state не создают транзакцию: authority обязан
сериализовать события и атомарно сохранить результат перед выдачей admission.

## Проверки

```sh
python -m unittest discover -s tools -p 'test_match*.py' -v
```

Тестируются правила и сохранение offline-состояния. Успех не доказывает
работоспособность FG, синхронизацию клиентов, persistent storage или античит.
В API нет сетевого listener, команды запуска сервера и автоматической
интеграции с transport/GUI.
