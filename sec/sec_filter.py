from config.db_config import get_db_connection

FILING_TABLE = "sec_filing"

# GPT 요약이 필요한 주가 영향 공시 (현재 정책)
GPT_FORMS = {
    "8-K",

    # 지분 / 수급
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",

    # 오퍼링 요약본
    "424B3",
    "424B5",

    # 경영 / 의결
    "DEF 14A",

    # 내부자 거래
    "4",
    "4/A",
}


def _get_filing_row(accession_no: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT id, notified_at FROM {FILING_TABLE} WHERE accession_no = %s LIMIT 1",
                (accession_no,)
            )
            return cursor.fetchone()
    finally:
        conn.close()


def check_filing_status(accession_no: str, form_type: str) -> dict:
    """
    SEC 공시 상태 판정 (판단만, 처리 X)

    already_saved:  sec_filing DB에 레코드가 이미 존재 (테스트 스크립트 등으로 삽입된 경우 포함)
    should_notify:  아직 알림을 보낸 적 없음 (notified_at IS NULL)
    should_gpt:     알림 대상이면서 GPT 요약 대상 폼 타입
    """
    accession_no = (accession_no or "").strip()
    form_type = (form_type or "").strip().upper()

    row = _get_filing_row(accession_no)

    already_saved = row is not None
    already_notified = already_saved and row["notified_at"] is not None

    should_notify = not already_notified
    should_gpt = should_notify and (form_type in GPT_FORMS)

    return {
        "should_notify": should_notify,
        "should_gpt": should_gpt,
        "already_saved": already_saved,
    }
