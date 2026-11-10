# Контекст проекта: Telemetry

Файл для быстрого восстановления контекста следующим агентом.
Все работы выполнялись в `/home/stas/Telemetry`.

## 1. Что это

Лёгкий сервис телеметрии для Linux по требованиям из `TASK.md`:
один процесс FastAPI + Uvicorn, сбор метрик через `psutil`, кольцевой буфер в
памяти, опциональный SQLite, REST + WebSocket, статический SPA-фронтенд.

`TASK.md` — исходное ТЗ, не переписывался. `README.md` — инструкция по
развёртыванию.

## 2. Текущее состояние

- Версия: `1.0.0`, пакет `telemetry`, модуль `telemetry`.
- Тесты: **54 passed** (`pytest`).
- Линтер: **ruff — all checks passed**.
- Сборка: wheel и sdist успешно собираются.
- Пробные запуски: REST, WebSocket, браузер, SQLite, auth — проверены.
- Серверы после проверок остановлены, порты `8765–8767` свободны.
- Git-репозитория в каталоге нет.

## 3. Структура

```text
/home/stas/Telemetry/
├── TASK.md                    # исходное ТЗ
├── README.md                  # запуск, env, systemd, примеры API
├── LICENSE                    # MIT
├── Makefile                   # install / test / lint / run / build / clean
├── pyproject.toml             # пакет, зависимости, pytest, ruff
├── requirements.txt
├── requirements-dev.txt
├── .gitignore
├── deploy/
│   └── telemetry.service      # systemd-юнит
├── telemetry/
│   ├── __init__.py            # __version__
│   ├── __main__.py            # python -m telemetry
│   ├── main.py                # CLI + uvicorn
│   ├── config.py              # Settings, env TELEMETRY_*
│   ├── models.py              # Pydantic-модели протокола
│   ├── collector.py           # psutil, температура, scheduler, broadcaster
│   ├── storage.py             # deque + asyncio.Lock + SQLite
│   ├── api.py                 # FastAPI, REST, WS, static, auth
│   └── frontend/
│       ├── index.html
│       ├── style.css
│       ├── app.js
│       └── vendor/chart.umd.min.js   # Chart.js 4.4.7, MIT, локально
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_storage.py
│   ├── test_collector.py
│   ├── test_api.py
│   └── test_websocket.py
├── dist/                      # артефакты сборки
└── .venv/                     # рабочее окружение (не входит в пакет)
```

## 4. Архитектура и ключевые решения

### Collector (`telemetry/collector.py`)

- `MetricsCollector.collect()` собирает точку:
  - CPU: `psutil.cpu_percent()` + `percpu=True`, load average через `os.getloadavg()`;
  - память/диск в МБ (MiB), сеть — накопительные байты;
  - температура: сначала sysfs `/sys/class/thermal/thermal_zone0/temp`,
    затем `psutil.sensors_temperatures()`, иначе `None`;
  - топ-процессы через `psutil.process_iter(attrs=...)`, сортировка по CPU,
    хранится не более `top_processes` (по умолчанию 5).
- Все подсистемы обёрнуты в `try/except`: недоступный датчик/счётчик не ломает
  точку, поля получают нули/`None`.
- Сбор выполняется в `asyncio.to_thread(self.collect)`, чтобы не блокировать
  event loop.
- Фоновый scheduler — `asyncio.create_task`; интервал меняется через
  `asyncio.Event` (`set_interval` будит цикл, новое значение применяется сразу).
- `MetricsBroadcaster` — fan-out в `asyncio.Queue` подписчиков WS; при
  переполнении очереди старые точки отбрасываются.
- `_prime()` при старте прогревает CPU/process counters psutil, чтобы первая
  точка не была нулевой.

### Storage (`telemetry/storage.py`)

- `collections.deque(maxlen=buffer_size)` + `asyncio.Lock`.
- `history(start, end, step)`:
  - фильтр включительно `start <= ts <= end`;
  - сортировка;
  - `step <= 1` — без прореживания;
  - иначе оставляет точки не чаще, чем раз в `step` секунд.
- SQLite опционален: таблица `metrics(ts REAL PRIMARY KEY, payload TEXT)`,
  JSON в payload, WAL, `synchronous=NORMAL`.
- Запись из `asyncio.to_thread`, flush раз в `sqlite_flush_sec` (30 с) и при
  `close()`.
- При flush БД обрезается до размера кольцевого буфера.
- `clear()` очищает и память, и SQLite (важно: `save([])` теперь делает
  `DELETE FROM metrics`).
- **Готча:** у `MetricsStorage` есть `__len__`, поэтому пустое хранилище
  ложно в boolean-контексте. В `api.py` использовать только
  `resolved_storage is not None`, не `resolved_storage or ...`.

### API (`telemetry/api.py`)

Префикс `/api/v1`:

| Метод | Путь | Поведение |
|---|---|---|
| GET | `/metrics` | последняя точка; `503`, если буфер пуст |
| GET | `/metrics/history` | `start`, `end` обязательны, `step>=1`; `start>end` → 400 |
| GET | `/processes` | `limit` 1..50, `sort=cpu\|memory`; `503`, если данных нет |
| GET | `/export` | `format=csv\|json`, attachment; CSV-колонки см. `CSV_COLUMNS` |
| POST | `/config` | `{"interval_sec": ...}`, валидация 0.5..3600 → 422 |
| GET | `/config` | текущий интервал, размер буфера, sqlite, версия |
| GET | `/health` | статус, интервал, число точек, версия; вне auth |
| WS | `/ws/metrics` | latest + поток точек, команда `set_interval:<sec>` |

- Экспорт CSV: `timestamp,cpu_percent,mem_percent,temp_c,disk_percent,net_sent,net_recv,mem_used_mb,mem_total_mb,disk_used_mb,disk_total_mb,net_packets_sent,net_packets_recv`.
- `Content-Disposition` без кавычек:
  `attachment; filename=telemetry_<start>_<end>.csv`.
- Временные метки в имени файла форматируются без scientific notation
  (`_timestamp_label`).
- Auth: если задан `TELEMETRY_TOKEN`, middleware требует
  `Authorization: Bearer <token>` для `/api/*`. Статика открыта. Для WS
  допускается `?token=<token>` или заголовок.
- WS при неверном токене закрывается до accept (код 4401; клиент `websockets`
  видит HTTP 403).
- WS-обработчик использует два task (sender/receiver) и общий `send_lock`.
  **Важно:** после отмены второго task нельзя делать `await gather(...)` —
  в Starlette TestClient это вызывает гонку и `concurrent.futures.CancelledError`
  на выходе из `websocket_connect`. Сейчас task только `cancel()`, без await.
- Документация: `/api/docs`, OpenAPI `/api/openapi.json` (под auth).
- Статика монтируется в `/` из `telemetry/frontend` (`StaticFiles(html=True)`).

### Config (`telemetry/config.py`)

`Settings` — dataclass, значения из env `TELEMETRY_*`:

| Переменная | По умолчанию |
|---|---|
| `TELEMETRY_HOST` | `0.0.0.0` |
| `TELEMETRY_PORT` | `8000` |
| `TELEMETRY_INTERVAL` | `2` |
| `TELEMETRY_MIN_INTERVAL` | `0.5` |
| `TELEMETRY_MAX_INTERVAL` | `3600` |
| `TELEMETRY_BUFFER_SIZE` | `3600` |
| `TELEMETRY_TOP_PROCESSES` | `5` |
| `TELEMETRY_SQLITE_PATH` | — |
| `TELEMETRY_SQLITE_FLUSH` | `30` |
| `TELEMETRY_TOKEN` | — |
| `TELEMETRY_FRONTEND_DIR` | `telemetry/frontend` |
| `TELEMETRY_START_COLLECTOR` | `true` |
| `TELEMETRY_LOG_LEVEL` | `info` |

CLI: `--host`, `--port`, `--interval`, `--buffer-size`, `--sqlite`, `--token`,
`--log-level`, `--version`.

### Frontend

- Тёмный адаптивный интерфейс, Chart.js 4.4.7 локально (офлайн).
- Карточки CPU/температура/RAM/диск/приём/отдача.
- Графики нагрузки (CPU/RAM/темп.) и сети (КБ/с).
- Таблица топ-процессов.
- Смена интервала через `POST /api/v1/config`.
- Экспорт CSV/JSON через `fetch` + blob (работает с Bearer-токеном).
- WebSocket с exponential backoff; при переподключении вызывает
  `loadHistory()` для догона пропущенных точек.
- Если в URL есть `?token=...`, токен добавляется в REST/WS/export.
- Ошибок в консоли браузера при проверке не было.

## 5. Тесты

Запуск:

```bash
cd /home/stas/Telemetry
.venv/bin/python -m pytest -q
```

Покрыто:

- `test_config.py` — env, границы интервала, невалидные значения.
- `test_storage.py` — latest/snapshot, ring buffer, history/step, preload,
  конкурентные добавления, SQLite roundtrip, фоновый flush, clear.
- `test_collector.py` — единицы, температура sysfs/psutil/None, graceful
  fallback, сортировка и лимит процессов, load average fallback, scheduler,
  set_interval, broadcaster.
- `test_api.py` — 503 на пустом буфере, метрики, history-валидация, процессы,
  config, CSV/JSON export, имя файла, статика, health, auth, default app.
- `test_websocket.py` — latest, команды, ошибки, ping, поток точек, auth.

Особенности тестовой инфраструктуры:

- `tests/conftest.py` — фикстуры `make_point`, `storage`, `collector`, `app`,
  `client`, `seed`, `seeded_client`.
- Синхронные тесты API используют `TestClient`; сидирование буфера:
  `client.portal.call(storage.add, point)`.
- Async-тесты — `pytest-asyncio`, `asyncio_mode = "auto"`.
- Для WS-потока используется `collect_func` с монотонными timestamp.
- `Settings(start_collector=False)` + `create_app(..., start_background=False)`
  отключают фоновый сборщик в тестах.

## 6. Сборка

```bash
cd /home/stas/Telemetry
.venv/bin/python -m build
```

Артефакты:

```text
dist/pi_telemetry-1.0.0-py3-none-any.whl
dist/pi_telemetry-1.0.0.tar.gz
```

В wheel включены `telemetry/frontend/*` и `frontend/vendor/chart.umd.min.js`
через `[tool.setuptools.package-data]`.

Проверка установленного пакета:

```bash
.venv/bin/python -m pip install --no-deps --force-reinstall dist/pi_telemetry-1.0.0-py3-none-any.whl
.venv/bin/python -c "from telemetry.config import Settings; print(Settings().frontend_dir)"
```

## 7. Запуск

```bash
cd /home/stas/Telemetry
.venv/bin/python -m telemetry --host 0.0.0.0 --port 8000
# или
.venv/bin/telemetry --host 0.0.0.0 --port 8000
```

Опции SQLite и токена:

```bash
.venv/bin/telemetry \
  --host 0.0.0.0 --port 8000 \
  --sqlite /var/lib/telemetry/metrics.db \
  --token change-me
```

`README.md` содержит установку, systemd и примеры `curl`.

## 8. Проведённые пробные запуски

- REST на `127.0.0.1:8765`: health, metrics, history, processes, export
  CSV/JSON, статика — 200, реальные значения (CPU, ~44 °C, RAM, диск, сеть).
- WebSocket: получение точек, `set_interval:0.5`, ошибка на `set_interval:0`.
- SQLite на `127.0.0.1:8766`: после graceful stop в БД 47 строк; повторный
  запуск загрузил историю (52 точки).
- Auth на `127.0.0.1:8767`: без токена REST 401, с токеном 200; WS без токена
  отклоняется, с `?token=` работает.
- Браузер: страница открывается, статус «онлайн», графики и процессы
  обновляются, интервал меняется, CSV скачивается, console errors — нет.

## 9. Важные ограничения и готчи

- Целевой Python — **3.9+**, фактически тестировалось на **3.14.4**.
  В коде намеренно используются `Optional[...]`, `List[...]`, `Dict[...]`,
  `from __future__ import annotations`. **Не запускать `ruff --fix` с
  правилом UP**: он предложит `X | None`, что сломает pydantic на Python 3.9.
- `python3-venv` в системе отсутствовал: `.venv` создан через
  `python3 -m venv --without-pip` и `get-pip.py`. При пересоздании окружения
  нужен `sudo apt install python3-venv` (см. `DEPLOY.md`).
- `aiofiles` из рекомендаций ТЗ не используется: экспорт формируется в памяти
  (`io.StringIO`), буфер ограничен, дополнительная зависимость не нужна.
- В точке хранится только `top_processes` (5 по умолчанию), поэтому
  `/processes?limit>5` вернёт максимум 5.
- Auth защищает только `/api/*`; `/health` и статика открыты.
- В `TASK.md` осталась исходная спецификация; не путать с документацией
  реализации.
- Фронтенд лежит в `telemetry/frontend`, а не в корне `frontend/`, чтобы
  попадать в wheel.
- Тесты WS чувствительны к cleanup-логике: не добавлять `await gather` после
  `cancel()` в `websocket_metrics`.
- В `api.py` нельзя использовать `storage or MetricsStorage(...)` из-за
  `__len__`; только проверку `is not None`.

