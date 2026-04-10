"""
SEC 공시 데이터를 저장함
"""

# sec_save.py
from config.db_config import get_db_connection

def save_filing_meta(meta: dict) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO sec_filing
              (cik10, ticker, accession_no, form_type, filing_date,
               accepted_at, primary_doc, filing_html_url, filing_txt_url, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            """
            cursor.execute(sql, (
                meta["cik10"],
                meta.get("ticker"),
                meta["accession_no"],
                meta["form_type"],
                meta["filing_date"],
                meta.get("accepted_at"),
                meta.get("primary_doc"),
                meta.get("filing_html_url"),
                meta.get("filing_txt_url"),
            ))
            conn.commit()

            # 방금 insert된 id 반환 (SELECT 재조회 불필요)
            return cursor.lastrowid
    finally:
        conn.close()

import os

def get_filing_id(accession_no: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM sec_filing WHERE accession_no = %s LIMIT 1",
                (accession_no,)
            )
            row = cursor.fetchone()
            if not row:
                raise KeyError(f"sec_filing row not found for {accession_no}")
            return row["id"]
    finally:
        conn.close()


def mark_filing_notified(accession_no: str) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE sec_filing SET notified_at = NOW() WHERE accession_no = %s AND notified_at IS NULL",
                (accession_no,)
            )
            conn.commit()
    finally:
        conn.close()


def save_filing_text(accession_no: str, content: str) -> str:
    """
    공시 본문을 로컬 텍스트 파일로 저장합니다.
    """
    storage_dir = os.path.join("storage", "filings")
    os.makedirs(storage_dir, exist_ok=True)
    
    file_path = os.path.join(storage_dir, f"{accession_no}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    return file_path

def get_filing_text(accession_no: str) -> str:
    """
    로컬에 저장된 공시 본문을 읽어옵니다.
    """
    file_path = os.path.join("storage", "filings", f"{accession_no}.txt")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def save_filing_content(filing_id: int, content_type: str, content: str) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO sec_filing_content (filing_id, content_type, content)
            VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE content=VALUES(content)
            """
            cursor.execute(sql, (filing_id, content_type, content))
            conn.commit()

            cursor.execute(
                "SELECT id FROM sec_filing_content WHERE filing_id=%s AND content_type=%s",
                (filing_id, content_type)
            )
            return cursor.fetchone()["id"]
    finally:
        conn.close()
