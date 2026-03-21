# manifest_parser_sync

Загрузчик `arXiv_pdf_manifest.xml` в PostgreSQL.

Скрипт читает XML-манифест arXiv с описанием tar-архивов PDF и заполняет таблицу `pdf_tar_manifest`. Эта таблица используется как карта tar-файлов: по ней можно понять, в каком архиве лежит диапазон arXiv id и какой tar уже обработан, находится в работе или упал с ошибкой.

---

## Что делает проект

`manifest_parser_sync.py`:

- потоково парсит `arXiv_pdf_manifest.xml`;
- извлекает метаданные по каждому tar: `tar_key`, `yymm`, `seq_num`, `first_item`, `last_item`, `num_items`, `size_bytes`, `timestamp_utc`, `content_md5sum`, `md5sum`;
- загружает данные через временный CSV + `COPY` во временную таблицу;
- затем делает `INSERT` или `UPSERT` в `pdf_tar_manifest`;
- обновляет `sync_state`, чтобы было видно статус последнего запуска.

Важно: при `--upsert` скрипт обновляет метаданные tar, но не сбрасывает рабочие статусы очереди (`DONE`, `PROCESSING`, `FAILED`) и не перетирает `last_error`.

---

## Где этот проект находится в пайплайне

Обычно порядок такой:

1. `oai_suprcon_sync` загружает список статей и базовые метаданные в `arxiv_paper`.
2. `manifest_parser_sync` загружает карту tar-архивов в `pdf_tar_manifest`.
3. `index_manifest_tars` читает сами tar-архивы из S3 и строит индекс `tar_key -> arxiv_id` в `pdf_tar_index`.
4. `tar_workers_sync` использует `arxiv_paper + pdf_tar_index`, скачивает нужные PDF из arXiv S3 и загружает их в целевое S3-хранилище.

---

## Требования

- Python 3.10+
- PostgreSQL
- `psycopg2-binary`

Установка:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install psycopg2-binary
```

Для Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -U pip
pip install psycopg2-binary
```

---

## Быстрый старт

### Upsert-загрузка

```bash
python manifest_parser_sync.py \
  --xml /data/arxiv/arXiv_pdf_manifest.xml \
  --pg postgresql://postgres:postgres@localhost:5432/rag \
  --upsert
```

### Полная перезаливка

```bash
python manifest_parser_sync.py \
  --xml /data/arxiv/arXiv_pdf_manifest.xml \
  --pg postgresql://postgres:postgres@localhost:5432/rag \
  --truncate
```

### Полная перезаливка с последующим upsert

Обычно не нужна. В типовом случае используют либо `--truncate`, либо `--upsert`.

---

## Аргументы CLI

```text
--xml       Путь к arXiv_pdf_manifest.xml
--pg        Postgres DSN
--truncate  Очистить pdf_tar_manifest перед загрузкой
--upsert    Делать ON CONFLICT DO UPDATE по tar_key
```

---

## Схема таблиц

### ENUM статусов tar

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tar_status') THEN
    CREATE TYPE tar_status AS ENUM ('NEW','PROCESSING','DONE','FAILED');
  END IF;
END $$;
```

### Основная таблица `pdf_tar_manifest`

```sql
CREATE TABLE IF NOT EXISTS public.pdf_tar_manifest (
  tar_key             text PRIMARY KEY,

  status              tar_status NOT NULL DEFAULT 'NEW',
  worker_id           text,
  locked_at           timestamptz,
  attempts            integer NOT NULL DEFAULT 0,
  last_error          text,

  yymm                char(4) NOT NULL,
  seq_num             int NOT NULL,
  first_item          text NOT NULL,
  last_item           text NOT NULL,
  num_items           int NOT NULL,
  size_bytes          bigint NOT NULL,
  timestamp_utc       timestamptz,
  content_md5sum      text,
  md5sum              text,

  num_items_indexed   integer,
  last_started_at     timestamptz,
  last_finished_at    timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
```

### Рекомендуемые индексы

```sql
CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_status_tar
  ON public.pdf_tar_manifest(status, tar_key);

CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_yymm_seq
  ON public.pdf_tar_manifest(yymm, seq_num);

CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_range
  ON public.pdf_tar_manifest(yymm, first_item, last_item);

CREATE INDEX IF NOT EXISTS idx_pdf_tar_manifest_locked_at_processing
  ON public.pdf_tar_manifest(locked_at)
  WHERE status = 'PROCESSING';

CREATE INDEX IF NOT EXISTS idx_manifest_pick
  ON public.pdf_tar_manifest(status, locked_at)
  WHERE status = 'NEW';
```

### Таблица состояния синхронизации

Если у вас её ещё нет, удобно использовать такую структуру:

```sql
CREATE TABLE IF NOT EXISTS sync_state (
  source                  text PRIMARY KEY,
  last_status             text,
  last_error              text,
  last_run_started_at     timestamptz,
  last_run_finished_at    timestamptz,
  last_success_at         timestamptz,
  last_success_datestamp  date,
  last_rows               bigint DEFAULT 0,
  total_rows              bigint DEFAULT 0,
  note                    text,
  updated_at              timestamptz NOT NULL DEFAULT now()
);
```

---

## Как это работает внутри

1. XML читается потоково через `xml.etree.ElementTree.iterparse`, поэтому файл не нужно целиком держать в памяти.
2. Для ускорения загрузки формируется временный CSV.
3. CSV загружается в temp-таблицу через `COPY`.
4. Из temp-таблицы данные вставляются в `pdf_tar_manifest`.
5. В `sync_state` записывается состояние запуска: `RUNNING`, `OK` или `ERROR`.

Такой подход обычно заметно быстрее, чем построчные insert'ы в Python.

---

## Типовые сценарии

### 1. Первый запуск

- создайте таблицы;
- скачайте актуальный `arXiv_pdf_manifest.xml`;
- выполните запуск с `--upsert`.

### 2. Регулярное обновление

- периодически скачивайте свежий manifest;
- запускайте скрипт по cron/systemd timer;
- используйте `--upsert`, чтобы не ломать уже обработанные статусы очереди.

### 3. Полная пересборка среды

- остановите downstream-воркеры;
- выполните `--truncate`;
- затем снова прогоните индексацию tar и загрузку PDF.

---

## Пример systemd unit

```ini
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
  --pg postgresql://postgres:postgres@localhost:5432/rag \
  --upsert
NoNewPrivileges=true
```

## Пример timer

```ini
[Unit]
Description=Run arXiv manifest loader daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

## Проверка результата

```sql
SELECT status, count(*)
FROM pdf_tar_manifest
GROUP BY status
ORDER BY status;
```

```sql
SELECT tar_key, yymm, first_item, last_item, num_items
FROM pdf_tar_manifest
ORDER BY yymm DESC, seq_num DESC
LIMIT 20;
```

```sql
SELECT *
FROM sync_state
WHERE source = 'manifest:arxiv_pdf_manifest';
```

---

## Возможные проблемы

### XML не найден

Проверьте путь в `--xml`.

### Ошибка подключения к PostgreSQL

Проверьте DSN и доступность базы.

### Статусы tar сбрасываются

Используйте `--upsert`, а не полную очистку таблицы, если downstream-воркеры уже работают.

### Manifest большой и загрузка долгая

Это нормально: проект специально использует потоковый парсинг и `COPY`, чтобы работать с крупными XML-файлами стабильнее.

---

## Идеи для развития

- вынести DDL в отдельные миграции;
- добавить `requirements.txt`;
- добавить dry-run режим;
- логировать количество новых и обновлённых tar отдельно;
- валидировать входной XML перед загрузкой.
