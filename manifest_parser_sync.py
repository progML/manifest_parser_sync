import argparse
import csv
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import psycopg2


SOURCE_KEY = "manifest:arxiv_pdf_manifest"


def parse_ts_utc(ts: str):
    """
    В manifest timestamp обычно вида: '2025-12-05 03:59:47'
    Храним как timestamptz в UTC.
    """
    ts = (ts or "").strip()
    if not ts:
        return None
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.isoformat()


def iter_manifest_rows(xml_path: str):
    """
    Стрим-парсер: на каждом </file> отдаёт строку.
    """
    context = ET.iterparse(xml_path, events=("end",))
    for _, elem in context:
        if elem.tag != "file":
            continue

        def txt(tag):
            c = elem.find(tag)
            return (c.text or "").strip() if c is not None and c.text else ""

        tar_key = txt("filename")
        yymm = txt("yymm")
        seq_num = txt("seq_num")
        first_item = txt("first_item")
        last_item = txt("last_item")
        num_items = txt("num_items")
        size_bytes = txt("size")
        timestamp = txt("timestamp")
        content_md5sum = txt("content_md5sum")
        md5sum = txt("md5sum")

        if not tar_key or not yymm:
            elem.clear()
            continue

        yield (
            tar_key,
            yymm,
            int(seq_num) if seq_num else 0,
            first_item,
            last_item,
            int(num_items) if num_items else 0,
            int(size_bytes) if size_bytes else 0,
            parse_ts_utc(timestamp),
            content_md5sum or None,
            md5sum or None,
        )

        elem.clear()


# -------------------- sync_state helpers --------------------

def ensure_sync_state(cur):
    cur.execute("""
        insert into sync_state(source, updated_at)
        values (%s, now())
        on conflict (source) do nothing
    """, (SOURCE_KEY,))


def mark_running(cur, note: str | None):
    cur.execute("""
        update sync_state
        set
          last_status = 'RUNNING',
          last_error = null,
          last_run_started_at = now(),
          last_run_finished_at = null,
          updated_at = now(),
          note = %s
        where source = %s
    """, (note, SOURCE_KEY))


def mark_success(cur, *, rows_written: int):
    cur.execute("""
        update sync_state
        set
          last_status = 'OK',
          last_error = null,
          last_run_finished_at = now(),
          last_success_at = now(),
          last_rows = %s,
          total_rows = total_rows + %s,
          updated_at = now()
        where source = %s
    """, (rows_written, rows_written, SOURCE_KEY))


def mark_error(cur, *, err: str):
    cur.execute("""
        update sync_state
        set
          last_status = 'ERROR',
          last_error = left(%s, 8000),
          last_run_finished_at = now(),
          last_rows = 0,
          updated_at = now()
        where source = %s
    """, (err, SOURCE_KEY))


def copy_via_temp_csv(conn, xml_path: str, truncate: bool, upsert: bool):
    """
    XML -> temp CSV -> COPY into staging -> INSERT/UPSERT into target
    """
    # 1) создаём временный CSV
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", delete=False, suffix=".csv") as tmp:
        csv_path = tmp.name
        w = csv.writer(tmp)
        w.writerow([
            "tar_key", "yymm", "seq_num", "first_item", "last_item",
            "num_items", "size_bytes", "timestamp_utc", "content_md5sum", "md5sum",
            "status", "last_error"
        ])
        for row in iter_manifest_rows(xml_path):
            # status NEW для новых строк, last_error пустой
            w.writerow((*row, "NEW", None))

    rows_written = 0

    try:
        with conn.cursor() as cur:
            ensure_sync_state(cur)
            mark_running(cur, note=f"xml={xml_path}, truncate={truncate}, upsert={upsert}")

            if truncate and not upsert:
                cur.execute("TRUNCATE TABLE pdf_tar_manifest CASCADE;")

            cur.execute("""
                create temporary table tmp_pdf_tar_manifest (
                  tar_key        text,
                  yymm           char(4),
                  seq_num        int,
                  first_item     text,
                  last_item      text,
                  num_items      int,
                  size_bytes     bigint,
                  timestamp_utc  timestamptz,
                  content_md5sum text,
                  md5sum         text,
                  status         text,
                  last_error     text
                ) on commit drop;
            """)

            copy_sql = """
                copy tmp_pdf_tar_manifest(
                    tar_key, yymm, seq_num, first_item, last_item,
                    num_items, size_bytes, timestamp_utc, content_md5sum, md5sum,
                    status, last_error
                )
                from stdin with (format csv, header true)
            """
            with open(csv_path, "r", encoding="utf-8") as f:
                cur.copy_expert(copy_sql, f)

            if upsert:
                # Важно: status и last_error НЕ перезатираем,
                # чтобы не сбрасывать DONE/PROCESSING/FAILED и не затирать причины ошибок воркера.
                cur.execute("""
                    insert into pdf_tar_manifest(
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum,
                        status, last_error, updated_at
                    )
                    select
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum,
                        status, last_error, now()
                    from tmp_pdf_tar_manifest
                    on conflict (tar_key) do update set
                        yymm = excluded.yymm,
                        seq_num = excluded.seq_num,
                        first_item = excluded.first_item,
                        last_item = excluded.last_item,
                        num_items = excluded.num_items,
                        size_bytes = excluded.size_bytes,
                        timestamp_utc = excluded.timestamp_utc,
                        content_md5sum = excluded.content_md5sum,
                        md5sum = excluded.md5sum,
                        updated_at = now();
                """)
            else:
                cur.execute("""
                    insert into pdf_tar_manifest(
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum,
                        status, last_error, updated_at
                    )
                    select
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum,
                        status, last_error, now()
                    from tmp_pdf_tar_manifest;
                """)

            cur.execute("select count(*) from tmp_pdf_tar_manifest;")
            rows_written = int(cur.fetchone()[0])

            mark_success(cur, rows_written=rows_written)

        conn.commit()
        return rows_written

    except Exception as e:
        conn.rollback()
        try:
            with conn.cursor() as cur:
                ensure_sync_state(cur)
                mark_error(cur, err=repr(e))
            conn.commit()
        except Exception:
            pass
        raise

    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True, help="Путь к arXiv_pdf_manifest.xml")
    ap.add_argument("--pg", required=True, help="Postgres DSN")
    ap.add_argument("--truncate", action="store_true", help="Очистить pdf_tar_manifest перед загрузкой (если без upsert)")
    ap.add_argument("--upsert", action="store_true", help="ON CONFLICT DO UPDATE по tar_key")
    args = ap.parse_args()

    if not os.path.exists(args.xml):
        print(f"XML not found: {args.xml}", file=sys.stderr)
        sys.exit(2)

    conn = psycopg2.connect(args.pg)
    conn.autocommit = False
    try:
        rows = copy_via_temp_csv(conn, args.xml, truncate=args.truncate, upsert=args.upsert)
    finally:
        conn.close()

    print(f"DONE: pdf_tar_manifest loaded. rows={rows}")


if __name__ == "__main__":
    main()
