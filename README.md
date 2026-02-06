# arXiv pdf manifest loader

Скрипт для загрузки `arXiv_pdf_manifest.xml` (manifest архива arXiv PDF tar) в PostgreSQL таблицу.


**Зачем это нужно:** воркеры, которые вытаскивают нужные PDF из `.tar` на arXiv, должны понимать **в каком tar лежит диапазон id** (first_item/last_item).  
Эта таблица — “карта tar-архивов”.
---

## Возможности

### Первичный запуск 

```bash
python .\manifest_parser_sync.py `
  --xml "C:\Users\User\Desktop\rag\arXiv_pdf_manifest.xml" `
  --pg "postgresql://postgres:postgres@localhost:5432/Rag" `
  --upsert
```
---

### Обновление сущностей

```bash
python .\manifest_parser_sync.py `
  --xml "C:\Users\User\Desktop\rag\arXiv_pdf_manifest.xml" `
  --pg "postgresql://postgres:postgres@localhost:5432/Rag" `
  --upsert
```
---

## Требования

- Python 3.10+ (рекомендуется 3.11/3.12/3.13)
- `psycopg2-binary`

---

##  Сущность в бд

---

### Используемые сущности


### Тип `pdf_tar_manifest`

Используется для хранения состояния обработки статьи.

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tar_status') THEN
    CREATE TYPE tar_status AS ENUM ('NEW','PROCESSING','DONE','FAILED');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.pdf_tar_manifest (
  tar_key        text PRIMARY KEY,                 -- pdf/arXiv_pdf_2511_041.tar

  -- очередь/воркеры
  status         tar_status NOT NULL DEFAULT 'NEW',
  worker_id      text,                             -- кто держит lease
  locked_at      timestamptz,                      -- lease timestamp
  attempts       integer NOT NULL DEFAULT 0,        -- сколько раз брали в работу
  last_error     text,

  -- метаданные из manifest
  yymm           char(4) NOT NULL,                 -- 2511
  seq_num        int NOT NULL,                     -- 41
  first_item     text NOT NULL,                    -- 2511.05538 или adap-org9801001
  last_item      text NOT NULL,
  num_items      int NOT NULL,
  size_bytes     bigint NOT NULL,
  timestamp_utc  timestamptz,                      -- время сборки/заливки (UTC)
  content_md5sum text,
  md5sum         text,

  -- метрики индексации
  num_items_indexed integer,                       -- сколько pdf реально увидели в tar
  last_started_at   timestamptz,
  last_finished_at  timestamptz,

  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- индексы под быстрый claim и админку
CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_status_tar
  ON public.pdf_tar_manifest(status, tar_key);

CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_yymm_seq
  ON public.pdf_tar_manifest(yymm, seq_num);

CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_range
  ON public.pdf_tar_manifest(yymm, first_item, last_item);

-- для revive (быстро находить протухшие leases)
CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_locked_at_processing
  ON public.pdf_tar_manifest(locked_at)
  WHERE status = 'PROCESSING';

```
---

## Разворачивание на сервере

### Установка зависимостей и подтягивание проекта

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
cd /opt
git clone https://github.com/progML/manifest_parser_sync.git
sudo chown -R "$USER":"$USER" /opt/manifest_parser_sync
cd /opt/manifest_parser_sync
```

### Создание виртуального окружения (если сделано в https://github.com/progML/oai_suprcon_sync можно не дублировать)

```bash 
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install requests psycopg2-binary
source .venv/bin/activate
pip install -U pip
pip install requests psycopg2-binary
deactivate
```

---
### Создание systemd service (юнит для запуска)

```bash
sudo nano /etc/systemd/system/manifest_parser_sync.service
```

Вставить следующие параметры

```
[Unit]
Description=Load arXiv PDF manifest into Postgres
After=network.target

[Service]
Type=oneshot
User=arxiv
Group=arxiv
WorkingDirectory=/opt/manifest_parser_sync

ExecStart=/opt/manifest_parser_sync/.venv/bin/python /opt/manifest_parser_sync/manifest_parser_sync.py \
  --xml /data/arxiv/arXiv_pdf_manifest.xml \
  --pg postgresql://postgres:postgres@localhost:5432/Rag \
  --upsert

NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Создание timer


```bash
/etc/systemd/system/manifest_parser_sync.timer
```

Вставить следующие параметры

```
[Unit]
Description=Run arXiv manifest loader daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

### Запуск

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now manifest_parser_sync.timer
systemctl list-timers | grep manifest_parser_sync
```

Проверка работы таймера

```bash
systemctl list-timers | grep manifest_parser_sync
```

Просмотр журнала/лог

```bash
journalctl -u manifest_parser_sync@$(whoami).service -n 100 --no-pager
journalctl -xeu manifest_parser_sync.service
```
