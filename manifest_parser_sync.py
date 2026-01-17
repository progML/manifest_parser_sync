import argparse
import csv
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import psycopg2


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
    Колонки под твою таблицу pdf_tar_manifest:
      tar_key, yymm, seq_num, first_item, last_item, num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
    """
    context = ET.iterparse(xml_path, events=("end",))
    for _, elem in context:
        if elem.tag != "file":
            continue

        def txt(tag):
            c = elem.find(tag)
            return (c.text or "").strip() if c is not None and c.text else ""

        tar_key = txt("filename")           # pdf/arXiv_pdf_2511_041.tar
        yymm = txt("yymm")                  # 2511
        seq_num = txt("seq_num")            # 41
        first_item = txt("first_item")      # 2511.05538 / adap-org9801001
        last_item = txt("last_item")
        num_items = txt("num_items")
        size_bytes = txt("size")
        timestamp = txt("timestamp")
        content_md5sum = txt("content_md5sum")
        md5sum = txt("md5sum")

        # базовая валидация
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


def copy_via_temp_csv(conn, xml_path: str, truncate: bool, upsert: bool):
    """
    Самый быстрый вариант:
    XML -> temp CSV -> COPY into staging -> INSERT (optionally upsert) into target
    """
    # 1) создаём временный CSV
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", delete=False, suffix=".csv") as tmp:
        csv_path = tmp.name
        w = csv.writer(tmp)
        w.writerow([
            "tar_key", "yymm", "seq_num", "first_item", "last_item",
            "num_items", "size_bytes", "timestamp_utc", "content_md5sum", "md5sum"
        ])
        for row in iter_manifest_rows(xml_path):
            w.writerow(row)

    try:
        with conn.cursor() as cur:
            if truncate and not upsert:
                # при upsert можно и не truncate, но иногда удобно
                cur.execute("truncate table pdf_tar_manifest;")

            # 2) staging таблица
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
                  md5sum         text
                ) on commit drop;
            """)

            # 3) COPY в staging
            copy_sql = """
                copy tmp_pdf_tar_manifest(
                    tar_key, yymm, seq_num, first_item, last_item,
                    num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
                )
                from stdin with (format csv, header true)
            """
            with open(csv_path, "r", encoding="utf-8") as f:
                cur.copy_expert(copy_sql, f)

            # 4) перенос в target
            if upsert:
                cur.execute("""
                    insert into pdf_tar_manifest(
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
                    )
                    select
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
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
                        md5sum = excluded.md5sum;
                """)
            else:
                # просто вставка (ожидаем пустую таблицу или уникальность)
                cur.execute("""
                    insert into pdf_tar_manifest(
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
                    )
                    select
                        tar_key, yymm, seq_num, first_item, last_item,
                        num_items, size_bytes, timestamp_utc, content_md5sum, md5sum
                    from tmp_pdf_tar_manifest;
                """)

        conn.commit()
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True, help="Путь к arXiv_pdf_manifest.xml")
    ap.add_argument("--pg", required=True, help="Postgres DSN, например: postgresql://user:pass@localhost:5432/db")
    ap.add_argument("--truncate", action="store_true", help="Очистить pdf_tar_manifest перед загрузкой (если без upsert)")
    ap.add_argument("--upsert", action="store_true", help="ON CONFLICT DO UPDATE по tar_key (можно без truncate)")
    args = ap.parse_args()

    if not os.path.exists(args.xml):
        print(f"XML not found: {args.xml}", file=sys.stderr)
        sys.exit(2)

    conn = psycopg2.connect(args.pg)
    conn.autocommit = False
    try:
        copy_via_temp_csv(conn, args.xml, truncate=args.truncate, upsert=args.upsert)
    finally:
        conn.close()

    print("DONE: pdf_tar_manifest loaded")


if __name__ == "__main__":
    main()


# С нуля заливка
# python manifestParser.py `
#   --xml "C:\Users\User\Desktop\rag\arXiv_pdf_manifest.xml" `
#   --pg "postgresql://postgres:postgres@localhost:5432/Rag" `
#   --truncate



# Апдейт
# python manifestParser.py `
#   --xml "C:\Users\User\Desktop\rag\arXiv_pdf_manifest.xml" `
#   --pg "postgresql://postgres:postgres@localhost:5432/Rag" `
#   --upsert




