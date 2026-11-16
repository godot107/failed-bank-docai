"""Review projection for the Power Apps review app: review.item + review.document, applied back by a stored procedure.

The review app (powerapps/) is a canvas app on Azure SQL. It works on a flat, reviewer-friendly projection of the
open queue rather than on the normalized desk.* tables:

  review.item       one row per open review item: field, extracted value, confidence, why it's flagged, help text,
                    the data-room value; the reviewer writes status / reviewed_value / reviewer / reviewed_at / comment.
                    System-versioned (temporal): every edit is kept in review.item_history.
  review.document   the page image (JPEG data URI, watermarked SYNTHETIC) for each document in the queue
  review.item_map   review.item ↔ desk.review_item, and when the decision was applied

  desk.apply_review_decisions   (stored procedure) takes every decided, not-yet-applied review.item and writes it
                    into the record: desk.review_decision, desk.review_item.status = 'decided', a new version of
                    desk.loan_field for corrections (source = reviewer), and desk.audit_event. A Power Automate flow
                    runs it on a schedule (powerapps/flows/), so the app never needs write access to desk.*.

Dates are stored as numbers YYYYMMDD in review.item (the app's value columns are numeric); the help text says so.
Usage: .venv/bin/python src/review_projection.py --reviewer <work account UPN>
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import ROOT, connect_sql
from normalize import FIELD_TYPES

SYN = ROOT / "samples" / "synthetic"
WATERMARK = "SYNTHETIC - fictional bank"
SHORT = {"promissory_note": "PN", "servicing_history": "SH", "appraisal_1004": "AP"}
LABELS = {
    "note_date": ("Note date", "Date the promissory note was signed."),
    "original_principal": ("Original principal", "Amount borrowed on the note (not the monthly payment)."),
    "note_rate_pct": ("Note rate (%)", "Interest rate on the note, in percent."),
    "monthly_payment": ("Monthly payment", "Scheduled monthly payment (principal + interest, or interest only)."),
    "maturity_date": ("Maturity date", "Date the loan is due in full."),
    "current_upb": ("Current unpaid balance", "Principal still owed as of the statement date."),
    "next_due_date": ("Next due date", "Date the next payment is due."),
    "appraised_value": ("Appraised value", "The appraiser's opinion of the property's value."),
}
REASONS = {
    "missing": "The model didn't find this value on the page.",
    "low_confidence": "The model wasn't confident in what it read.",
    "rule_failure": "The value broke a validity rule (range, date order, payment vs principal, loan-to-value).",
    "mismatch": "The value disagrees with the data-room schedule from the servicing system.",
}

DDL = [
    "IF SCHEMA_ID('review') IS NULL EXEC('CREATE SCHEMA review')",
    """IF OBJECT_ID('review.item') IS NULL CREATE TABLE review.item (
        item_id INT IDENTITY PRIMARY KEY,
        doc_id NVARCHAR(16) NOT NULL, method NVARCHAR(32) NOT NULL, field NVARCHAR(64) NOT NULL,
        carrier NVARCHAR(128), layout NVARCHAR(32),
        extracted_value FLOAT NULL, confidence FLOAT NULL, flag_reasons NVARCHAR(256),
        status NVARCHAR(20) NOT NULL DEFAULT 'open'
            CONSTRAINT ck_item_status CHECK (status IN ('open', 'accepted', 'corrected', 'not_on_document')),
        reviewed_value FLOAT NULL, reviewer NVARCHAR(256) NULL, reviewed_at DATETIME2 NULL, comment NVARCHAR(400) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        field_label NVARCHAR(600) NULL, field_help NVARCHAR(600) NULL, reason_help NVARCHAR(600) NULL,
        hint NVARCHAR(600) NULL, doc_context NVARCHAR(600) NULL, other_values NVARCHAR(600) NULL,
        valid_from DATETIME2 GENERATED ALWAYS AS ROW START HIDDEN NOT NULL CONSTRAINT df_item_from DEFAULT SYSUTCDATETIME(),
        valid_to DATETIME2 GENERATED ALWAYS AS ROW END HIDDEN NOT NULL
            CONSTRAINT df_item_to DEFAULT CONVERT(DATETIME2, '9999-12-31 23:59:59.9999999'),
        PERIOD FOR SYSTEM_TIME (valid_from, valid_to))
     WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = review.item_history))""",
    """IF OBJECT_ID('review.document') IS NULL CREATE TABLE review.document (
        doc_id NVARCHAR(16) PRIMARY KEY, page_image NVARCHAR(MAX) NOT NULL)""",
    """IF OBJECT_ID('review.item_map') IS NULL CREATE TABLE review.item_map (
        item_id INT PRIMARY KEY, desk_item_id BIGINT NOT NULL, loan_id INT NOT NULL, field NVARCHAR(60) NOT NULL,
        field_type NVARCHAR(10) NOT NULL, applied_at DATETIME2 NULL)""",
    "IF COL_LENGTH('review.item_map', 'applied_status') IS NULL ALTER TABLE review.item_map ADD applied_status NVARCHAR(20) NULL, applied_value FLOAT NULL",
]

APPLY_PROC = r"""
CREATE OR ALTER PROCEDURE desk.apply_review_decisions AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRAN;
    -- one run at a time: the scheduled flow and a manual run must not both apply the same decision (REVIEW.md D2)
    EXEC sp_getapplock @Resource = 'desk.apply_review_decisions', @LockMode = 'Exclusive', @LockOwner = 'Transaction';

    DECLARE @todo TABLE (item_id INT, desk_item_id BIGINT, loan_id INT, field NVARCHAR(60), field_type NVARCHAR(10),
                         status NVARCHAR(20), reviewed_value FLOAT, reviewer NVARCHAR(256), comment NVARCHAR(400),
                         decided_at DATETIME2, value NVARCHAR(40), redecision BIT);
    -- decided and never applied, OR changed again after it was applied (a reviewer changed their mind)
    INSERT INTO @todo
    SELECT i.item_id, m.desk_item_id, m.loan_id, m.field, m.field_type, i.status, i.reviewed_value,
           COALESCE(i.reviewer, 'unknown'), i.comment, i.valid_from,      -- system time, UTC (reviewed_at is the app's local time)
           CASE WHEN i.reviewed_value IS NULL THEN NULL
                WHEN m.field_type = 'date'
                THEN CONVERT(NVARCHAR(10), TRY_CONVERT(date, CONVERT(NVARCHAR(8), TRY_CAST(i.reviewed_value AS BIGINT)), 112), 23)
                ELSE CONVERT(NVARCHAR(40), CAST(i.reviewed_value AS DECIMAL(19, 4))) END,
           CASE WHEN m.applied_at IS NULL THEN 0 ELSE 1 END
    FROM review.item i JOIN review.item_map m ON m.item_id = i.item_id
    -- re-apply when the decision itself changed, not by comparing clocks (temporal row times are transaction start
    -- times, and the app's reviewed_at is local time)
    WHERE i.status <> 'open'
      AND (m.applied_at IS NULL OR i.status <> m.applied_status
           OR ISNULL(i.reviewed_value, -1e300) <> ISNULL(m.applied_value, -1e300));

    -- a correction whose value can't be read (e.g. a date that isn't YYYYMMDD) goes back to the reviewer instead of
    -- silently becoming NULL in the loan tape (REVIEW.md D3)
    UPDATE i SET status = 'open',
                 comment = LEFT(CONCAT('Could not read "', CONVERT(NVARCHAR(40), CAST(t.reviewed_value AS DECIMAL(19, 2))),
                                      '": enter dates as YYYYMMDD. ', i.comment), 400)
    FROM review.item i JOIN @todo t ON t.item_id = i.item_id
    WHERE t.status = 'corrected' AND t.value IS NULL;
    INSERT INTO desk.audit_event (actor, action, entity, entity_id, before_json, after_json)
    SELECT 'apply_review_decisions', 'invalid_review_value', 'review.item', CONVERT(NVARCHAR(60), item_id), NULL,
           CONCAT('{"reviewed_value":', COALESCE(CONVERT(NVARCHAR(40), reviewed_value), 'null'), '}')
    FROM @todo WHERE status = 'corrected' AND value IS NULL;
    DELETE FROM @todo WHERE status = 'corrected' AND value IS NULL;

    INSERT INTO desk.review_decision (item_id, decision, new_value, reviewer, comment, decided_at)
    SELECT desk_item_id,
           CASE status WHEN 'accepted' THEN 'confirm' WHEN 'corrected' THEN 'correct' ELSE 'not_on_document' END,
           CASE WHEN status = 'corrected' THEN value END, reviewer, comment, decided_at
    FROM @todo;

    UPDATE d SET status = 'decided' FROM desk.review_item d JOIN @todo t ON t.desk_item_id = d.item_id;

    -- corrections and "not on document" become a new version of the loan-tape value (the temporal table keeps the
    -- model's value in history); "not on document" clears the value instead of leaving the model's guess (REVIEW.md D4)
    MERGE desk.loan_field AS f
    USING (SELECT loan_id, field, reviewer, comment, status,
                  CASE WHEN status = 'corrected' THEN value END AS value
           FROM @todo WHERE status IN ('corrected', 'not_on_document')) AS c
    ON f.loan_id = c.loan_id AND f.field = c.field
    WHEN MATCHED THEN UPDATE SET value = c.value, source = 'reviewer', changed_by = c.reviewer,
                                 reason = COALESCE(c.comment, CASE c.status WHEN 'corrected' THEN 'corrected in review'
                                                                            ELSE 'not on document' END)
    WHEN NOT MATCHED THEN   -- also records "not on document" for a field the model never found
        INSERT (loan_id, field, value, source, extraction_id, changed_by, reason)
        VALUES (c.loan_id, c.field, c.value, 'reviewer', NULL, c.reviewer,
                COALESCE(c.comment, CASE c.status WHEN 'corrected' THEN 'corrected in review' ELSE 'not on document' END));

    -- a confirmed value is now trusted: record who confirmed it
    UPDATE f SET reason = 'confirmed in review', changed_by = t.reviewer
    FROM desk.loan_field f JOIN @todo t ON t.loan_id = f.loan_id AND t.field = f.field WHERE t.status = 'accepted';

    INSERT INTO desk.audit_event (actor, action, entity, entity_id, before_json, after_json)
    SELECT reviewer, CASE WHEN redecision = 1 THEN 'review_changed_' ELSE 'review_' END + status, 'desk.review_item',
           CONVERT(NVARCHAR(60), desk_item_id), NULL,
           CONCAT('{"field":"', field, '","value":', COALESCE('"' + value + '"', 'null'), '}')
    FROM @todo;

    UPDATE m SET applied_at = SYSUTCDATETIME(), applied_status = t.status, applied_value = t.reviewed_value
    FROM review.item_map m JOIN @todo t ON t.item_id = m.item_id;
    COMMIT;
    SELECT COUNT(*) AS applied FROM @todo;
END
"""


def page_image(pdf: Path, width: int = 900) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", "-singlefile", str(pdf), f"{tmp}/page"],
                       check=True)
        im = Image.open(f"{tmp}/page.png").convert("RGB")
    im.thumbnail((width, width * 2))
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(im.width / 14))
    except OSError:
        font = ImageFont.load_default()
    w, h = d.textbbox((0, 0), WATERMARK, font=font)[2:]
    d.text(((im.width - w) / 2, (im.height - h) / 2), WATERMARK, font=font, fill=(200, 30, 30, 75))
    layer = layer.rotate(25, center=(im.width / 2, im.height / 2))
    im = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def as_number(field: str, v):
    """Numbers stay numbers; ISO dates become YYYYMMDD numbers for the app's numeric value columns."""
    if v in (None, ""):
        return None
    if FIELD_TYPES.get(field) == "date":
        return float(str(v).replace("-", "")[:8]) if len(str(v)) >= 10 else None
    try:
        return float(v)
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reviewer", required=True, help="work-account UPN that uses the app")
    a = ap.parse_args()
    conn = connect_sql()
    cur = conn.cursor()
    for s in DDL:
        cur.execute(s)
    cur.execute(APPLY_PROC)
    conn.commit()
    # rebuild the projection from the open desk queue (decided items are kept in desk.* and review.item_history)
    # decisions not yet applied would be lost by a rebuild: apply them first (REVIEW.md D1)
    cur.execute("EXEC desk.apply_review_decisions")
    cur.fetchall()
    # rebuild; review.item keeps system versioning on, so every past row stays in review.item_history
    for t in ("review.item_map", "review.item", "review.document"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("""
        SELECT i.item_id, i.loan_id, i.field_or_flag, i.reason, b.name, b.layout_family, l.loan_number,
               e.raw_value, e.normalized_value, e.confidence, e.model_id, d.doc_type, lf.blob_path
        FROM desk.review_item i
        JOIN desk.loan l ON l.loan_id = i.loan_id JOIN desk.bank b ON b.bank_id = l.bank_id
        OUTER APPLY (SELECT TOP 1 x.raw_value, x.normalized_value, x.confidence, x.model_id, x.doc_id FROM desk.extraction x
                     WHERE x.loan_id = i.loan_id AND x.field = i.field_or_flag ORDER BY x.run_at DESC) e
        LEFT JOIN desk.document d ON d.doc_id = e.doc_id
        LEFT JOIN desk.loan_file lf ON lf.file_id = d.file_id
        WHERE i.status <> 'decided'""")
    rows = cur.fetchall()
    images = {}
    n = 0
    for (desk_id, loan_id, field, reason, bank, style, loan_no, raw, norm, conf, model, doc_type, blob) in rows:
        loan_key = Path(blob).stem if blob else None                      # e.g. B5-0012
        doc_type = doc_type or next((dt for dt, fs in (("promissory_note", ("note_date", "original_principal", "note_rate_pct",
                                     "monthly_payment", "maturity_date")), ("servicing_history", ("current_upb", "next_due_date")),
                                     ("appraisal_1004", ("appraised_value",))) if field in fs), "promissory_note")
        doc_id = f"{loan_key}-{SHORT.get(doc_type, 'XX')}"[:16] if loan_key else f"L{loan_id}"
        if loan_key and doc_id not in images:
            pdf = SYN / "docs" / f"{loan_key}__{doc_type}.pdf"
            if pdf.exists():
                images[doc_id] = page_image(pdf)
                cur.execute("INSERT INTO review.document (doc_id, page_image) VALUES (?, ?)", (doc_id, images[doc_id]))
        lab, help_ = LABELS.get(field, (field, ""))
        is_date = FIELD_TYPES.get(field) == "date"
        dr = ""
        if loan_key:
            labp = SYN / "labels" / f"{loan_key}.json"
            if labp.exists():
                dr_val = json.loads(labp.read_text())["data_room"].get(field)
                dr = f"Data-room schedule says: {dr_val}" if dr_val is not None else ""
        cur.execute("""INSERT INTO review.item (doc_id, method, field, carrier, layout, extracted_value, confidence, flag_reasons,
                       field_label, field_help, reason_help, hint, doc_context, other_values)
                       OUTPUT INSERTED.item_id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (doc_id, model or "model", field, bank, style, as_number(field, norm), conf, reason,
                     lab, help_ + (" Enter dates as YYYYMMDD (e.g. 20080406)." if is_date else ""),
                     REASONS.get(reason, reason), f"Printed on the page as: {raw}" if raw else "Not found on the page.",
                     f"Loan {loan_no} · {doc_type.replace('_', ' ')} · {bank}", dr))
        item_id = cur.fetchone()[0]
        cur.execute("INSERT INTO review.item_map (item_id, desk_item_id, loan_id, field, field_type) VALUES (?, ?, ?, ?, ?)",
                    (item_id, desk_id, loan_id, field, "date" if is_date else "number"))
        n += 1
    # least privilege for the app's work account
    upn = a.reviewer
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", upn):   # REVIEW.md D5
        raise SystemExit(f"--reviewer must be a user principal name (user@domain): {upn!r}")
    q = upn.replace("]", "]]")                                                   # bracket-quote the principal
    cur.execute("IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = ?) "
                f"EXEC('CREATE USER [{q}] FROM EXTERNAL PROVIDER')", (upn,))
    cur.execute(f"GRANT SELECT ON review.item TO [{q}]")
    cur.execute(f"GRANT SELECT ON review.document TO [{q}]")
    cur.execute(f"GRANT UPDATE ON review.item (status, reviewed_value, reviewer, reviewed_at, comment) TO [{q}]")
    cur.execute(f"GRANT EXECUTE ON desk.apply_review_decisions TO [{q}]")     # run by the Power Automate flow
    conn.commit()
    print(f"review.item: {n} open items; review.document: {len(images)} page images; grants for {upn}")


if __name__ == "__main__":
    main()
