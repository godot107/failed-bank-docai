-- Prescriptive review queue (src/prescribe.py documents the method and keeps a Python reference implementation;
-- `prescribe.py refresh` checks the two agree).
--   exposure = P(value is wrong | why it was routed) x loan balance at stake; highest first.
--   P(wrong): Beta(1,1)-smoothed rate per routing reason from other banks' outcomes plus this bank's reviewer
--   decisions (never this bank's evaluation answers: they are the key for its open items). Missing value = 1.
-- review.tr_item_priority re-ranks the queue whenever a reviewer saves a decision: no flow or timer needed.
IF OBJECT_ID('review.item_priority') IS NULL
CREATE TABLE review.item_priority (
    item_id        int NOT NULL CONSTRAINT pk_item_priority PRIMARY KEY,
    priority_rank  int NOT NULL,
    loan_number    nvarchar(40) NOT NULL,
    bank           nvarchar(120) NOT NULL,
    field_label    nvarchar(80) NULL,
    reason         nvarchar(60) NULL,
    p_wrong        float NOT NULL,          -- smoothed; other banks + this bank's reviewer decisions
    p_basis        nvarchar(80) NOT NULL,   -- e.g. '5 wrong of 19 labelled'
    upb            float NULL,              -- loan balance at stake
    exposure_usd   float NOT NULL,          -- p_wrong x upb
    changes_pool   bit NOT NULL,            -- next-due date decides the sale pool
    refreshed_at   datetime2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
CREATE OR ALTER PROCEDURE review.refresh_item_priority AS
BEGIN
    SET NOCOUNT ON;
    WITH items AS (
        SELECT i.item_id, i.status, i.flag_reasons AS reason, i.field, i.field_label, l.loan_id, l.loan_number, l.bank,
               CASE WHEN i.status IN ('corrected', 'not_on_document') THEN 1 WHEN i.status = 'accepted' THEN 0
                    WHEN o.outcome IS NULL THEN NULL WHEN o.outcome = 'correct' THEN 0 ELSE 1 END AS wrong,
               CASE WHEN i.status <> 'open' THEN 'reviewer' ELSE 'evaluation' END AS src
        FROM review.item i
        JOIN review.loan l ON l.file_key = LEFT(i.doc_id, LEN(l.file_key)) OR i.doc_id = CONCAT('L', l.loan_id)
        LEFT JOIN eval.field_outcome o ON o.loan_id = l.loan_id AND o.field = i.field AND o.routed = 'review'
    ),
    labels AS (   -- a combined reason ('a;b') counts toward itself and each part
        SELECT DISTINCT it.item_id, it.bank, it.src, it.wrong, k.rkey
        FROM items it
        CROSS APPLY (SELECT it.reason AS rkey UNION SELECT value FROM STRING_SPLIT(it.reason, ';')) k
        WHERE it.wrong IS NOT NULL AND it.reason IS NOT NULL
    ),
    scored AS (
        SELECT oi.*, u.upb, ex.w AS ex_w, ex.n AS ex_n, pt.w AS pt_w, pt.n AS pt_n
        FROM items oi
        OUTER APPLY (SELECT TRY_CAST(REPLACE(COALESCE(NULLIF(d.schedule_text, ''), d.value_text), ',', '') AS float) AS upb
                     FROM review.loan_detail d WHERE d.loan_id = oi.loan_id AND d.kind = 'field' AND d.field = 'current_upb') u
        OUTER APPLY (SELECT SUM(lb.wrong) AS w, COUNT(*) AS n FROM labels lb
                     WHERE lb.rkey = oi.reason AND (lb.bank <> oi.bank OR lb.src = 'reviewer')) ex
        OUTER APPLY (SELECT TOP 1 x.w, x.n FROM (
                         SELECT s.value AS part, SUM(lb.wrong) AS w, COUNT(*) AS n
                         FROM STRING_SPLIT(oi.reason, ';') s
                         JOIN labels lb ON lb.rkey = s.value AND (lb.bank <> oi.bank OR lb.src = 'reviewer')
                         GROUP BY s.value) x
                     ORDER BY (x.w + 1.0) / (x.n + 2.0) DESC) pt
        WHERE oi.status = 'open'
    ),
    final AS (
        SELECT item_id, loan_number, bank, field_label, reason, upb, field,
               CASE WHEN reason = 'missing' THEN 1.0
                    WHEN ex_n >= 3 THEN (ex_w + 1.0) / (ex_n + 2.0)
                    WHEN pt_n IS NOT NULL THEN (pt_w + 1.0) / (pt_n + 2.0)
                    ELSE 0.5 END AS p_wrong,
               CASE WHEN reason = 'missing' THEN N'value missing: the tape has no value'
                    WHEN ex_n >= 3 THEN CONCAT(ex_w, N' wrong of ', ex_n, N' labelled')
                    WHEN pt_n IS NOT NULL THEN CONCAT(pt_w, N' wrong of ', pt_n, N' labelled (few labels)')
                    ELSE N'no data yet (prior)' END AS p_basis
        FROM scored
    )
    SELECT item_id, loan_number, bank, field_label, reason, upb, field, p_wrong, p_basis,
           p_wrong * COALESCE(upb, 0) AS exposure_usd
    INTO #p FROM final;

    DELETE FROM review.item_priority;
    INSERT INTO review.item_priority (item_id, priority_rank, loan_number, bank, field_label, reason, p_wrong, p_basis,
                                      upb, exposure_usd, changes_pool)
    SELECT item_id, ROW_NUMBER() OVER (ORDER BY exposure_usd DESC, item_id), loan_number, bank, field_label, reason,
           ROUND(p_wrong, 4), p_basis, upb, ROUND(exposure_usd, 2), CASE WHEN field = 'next_due_date' THEN 1 ELSE 0 END
    FROM #p;
END;
GO
CREATE OR ALTER TRIGGER review.tr_item_priority ON review.item AFTER INSERT, UPDATE AS
BEGIN
    SET NOCOUNT ON;
    IF UPDATE(status) EXEC review.refresh_item_priority;
END;
GO
