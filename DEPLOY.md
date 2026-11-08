# Развёртывание

## Быстрый запуск

```bash
cd /home/stas/Telemetry
sudo apt install -y python3-venv   # Debian/Ubuntu, if venv is unavailable
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m telemetry --host 0.0.0.0 --port 8000
```

Откройте `http://<host>:8000/`. REST-документация — `http://<host>:8000/api/docs`.

## Параметры

Все настройки читаются из переменных окружения `TELEMETRY_*` и могут быть
переопределены аргументами командной строки.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEMETRY_HOST` | `0.0.0.0` | Адрес прослушивания |
| `TELEMETRY_PORT` | `8000` | Порт |
| `TELEMETRY_INTERVAL` | `2` | Интервал сбора, секунды |
| `TELEMETRY_BUFFER_SIZE` | `3600` | Размер кольцевого буфера, точек |
| `TELEMETRY_TOP_PROCESSES` | `5` | Сколько процессов хранить в точке |
| `TELEMETRY_SQLITE_PATH` | — | Включить SQLite-персистентность |
| `TELEMETRY_SQLITE_FLUSH` | `30` | Период сброса в SQLite, секунды |
| `TELEMETRY_TOKEN` | — | Bearer-токен для `/api/*` и `/ws/metrics` |
| `TELEMETRY_START_COLLECTOR` | `true` | Запускать фоновый сборщик |
| `TELEMETRY_LOG_LEVEL` | `info` | Уровень логирования |

Аргументы CLI: `--host`, `--port`, `--interval`, `--buffer-size`, `--sqlite`,
`--token`, `--log-level`.

## systemd

```bash
sudo useradd --system --home /opt/telemetry --shell /usr/sbin/nologin pi
sudo mkdir -p /opt/telemetry
sudo rsync -a --exclude .venv ./ /opt/telemetry/
sudo -u pi python3 -m venv /opt/telemetry/.venv
sudo -u pi /opt/telemetry/.venv/bin/pip install /opt/telemetry
sudo cp deploy/telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telemetry
```

Проверка:

```bash
systemctl status telemetry
curl http://127.0.0.1:8000/health
```

## Проверка API

```bash
curl http://127.0.0.1:8000/api/v1/metrics
curl 'http://127.0.0.1:8000/api/v1/metrics/history?start=0&end=9999999999&step=5'
curl -X POST http://127.0.0.1:8000/api/v1/config \
     -H 'Content-Type: application/json' -d '{"interval_sec": 2}'
curl -OJ 'http://127.0.0.1:8000/api/v1/export?start=0&end=9999999999&format=csv'
```

Если задан `TELEMETRY_TOKEN`, добавьте заголовок
`Authorization: Bearer <token>` (для WebSocket допускается `?token=<token>`).
