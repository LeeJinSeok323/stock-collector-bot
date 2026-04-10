import traceback
from datetime import date, timedelta
import sec.sec_fetch as fetch
import sec.sec_filter as filter
import sec.sec_save as save
from config.messages import M

NO_MEANING_ALERT_DATE_COUNT = 2

def check_new_filings(ticker: str):
    """
    특정 티커의 SEC 공시를 확인하고 새로운 공시가 있으면 DB에 저장한 뒤,
    새로운 공시 정보 리스트를 반환합니다.
    """
    ticker = ticker.upper()
    try:
        submission = fetch.get_sec_submissions(ticker)
    except Exception as e:
        print(M["LOG_SEC_ERR_FETCH"].format(ticker=ticker, err=e))
        return []
    
    recent = submission.get("filings", {}).get("recent", {})
    if not recent:
        return []

    cutoff = (date.today() - timedelta(days=NO_MEANING_ALERT_DATE_COUNT)).strftime("%Y-%m-%d")
    
    try:
        cik = str(submission.get("cik", "")).lstrip("0")
        cik10 = fetch.get_cik10(ticker)
    except Exception as e:
        print(M["LOG_SEC_ERR_CIK"].format(ticker=ticker, err=e))
        return []

    new_filings = []

    for accession_no, form_type, filing_date_str, primary_doc, accepted_at in zip(
        recent.get("accessionNumber", []),
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("primaryDocument", []),
        recent.get("acceptanceDateTime", []),
    ):
        if filing_date_str < cutoff:
            break

        status = filter.check_filing_status(accession_no, form_type)
        if not status["should_notify"]:
            continue

        try:
            full_text = fetch.get_filing_detail(submission, accession_no)
            save.save_filing_text(accession_no, full_text)
        except Exception as e:
            print(f"Failed to save local text for {accession_no}: {e}")

        acc_no_clean = accession_no.replace("-", "")
        base_dir = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_clean}"

        accepted_at_mysql = None
        if accepted_at:
            accepted_at_mysql = accepted_at.replace("T", " ").replace("Z", "")[:19]

        meta = {
            "cik10": cik10,
            "ticker": ticker,
            "accession_no": accession_no,
            "form_type": form_type,
            "filing_date": filing_date_str,
            "accepted_at": accepted_at_mysql,
            "primary_doc": primary_doc,
            "filing_html_url": f"{base_dir}/{primary_doc}" if primary_doc else None,
            "filing_txt_url": f"{base_dir}/{acc_no_clean}.txt",
        }

        try:
            if status["already_saved"]:
                filing_id = save.get_filing_id(accession_no)
            else:
                filing_id = save.save_filing_meta(meta)
                print(M["LOG_SEC_SAVED"].format(access_no=accession_no, ticker=ticker))
            meta["filing_id"] = filing_id
            new_filings.append(meta)
        except Exception as e:
            print(M["LOG_SEC_ERR_SAVE"].format(access_no=accession_no, ticker=ticker, err=e))
            traceback.print_exc()

    return new_filings
