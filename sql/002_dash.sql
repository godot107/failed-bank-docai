-- Dashboard layer (docs/DASHBOARD.md): answer-key outcomes (synthetic data only), public reference data, and the
-- views Power BI and Power Apps read. Re-runnable.

IF SCHEMA_ID('eval') IS NULL EXEC('CREATE SCHEMA eval');
IF SCHEMA_ID('ref')  IS NULL EXEC('CREATE SCHEMA ref');
IF SCHEMA_ID('dash') IS NULL EXEC('CREATE SCHEMA dash');
GO

-- widen the checks for the holdout bank and "missing" review reason
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'ck_bank_split')
    ALTER TABLE desk.bank DROP CONSTRAINT ck_bank_split;
ALTER TABLE desk.bank ADD CONSTRAINT ck_bank_split CHECK (split IN ('train','val','test','holdout','holdout2'));
DECLARE @ck sysname = (SELECT name FROM sys.check_constraints
                       WHERE parent_object_id = OBJECT_ID('desk.review_item') AND definition LIKE '%low_confidence%');
IF @ck IS NOT NULL EXEC('ALTER TABLE desk.review_item DROP CONSTRAINT ' + @ck);
ALTER TABLE desk.review_item ADD CONSTRAINT ck_review_reason
    CHECK (reason IN ('missing','low_confidence','rule_failure','mismatch','burn_in','text_flag'));
GO

-- Answer key: only exists because the loan files are synthetic. In production this table is replaced by reviewer
-- decisions (a value a reviewer corrected = an error the model made).
IF OBJECT_ID('eval.field_outcome') IS NULL
CREATE TABLE eval.field_outcome (
    loan_id        int NOT NULL,
    bank_id        int NOT NULL,
    field          nvarchar(60) NOT NULL,
    model_id       nvarchar(100) NOT NULL,
    outcome        nvarchar(10) NOT NULL,   -- correct / wrong / missing
    routed         nvarchar(10) NOT NULL,   -- auto / review
    reasons        nvarchar(100) NULL,
    silent_error   bit NOT NULL,            -- wrong AND auto-accepted
    confidence     decimal(5,4) NULL,
    scanned        bit NOT NULL,
    CONSTRAINT pk_field_outcome PRIMARY KEY (loan_id, field, model_id)
);

IF OBJECT_ID('ref.failure_year') IS NULL
CREATE TABLE ref.failure_year (
    year           int PRIMARY KEY,
    failures       int NOT NULL,
    assistance     int NOT NULL,
    est_cost_musd  float NULL,             -- FDIC estimated loss to the insurance fund, $ millions
    wave           nvarchar(20) NULL       -- '2008-2012' / '2023' / NULL
);
GO

CREATE OR ALTER VIEW dash.v_bank AS
SELECT b.bank_id, b.name, b.split, b.layout_family, b.status, b.closing_date,
       COUNT(DISTINCT l.loan_id) AS loans,
       (SELECT COUNT(*) FROM eval.field_outcome o WHERE o.bank_id = b.bank_id) AS field_values,
       (SELECT COUNT(*) FROM desk.review_item i JOIN desk.loan l2 ON l2.loan_id = i.loan_id
         WHERE l2.bank_id = b.bank_id AND i.status <> 'decided') AS open_items
FROM desk.bank b LEFT JOIN desk.loan l ON l.bank_id = b.bank_id
GROUP BY b.bank_id, b.name, b.split, b.layout_family, b.status, b.closing_date;
GO

CREATE OR ALTER VIEW dash.v_quality AS
SELECT b.name AS bank, b.split, o.field, o.model_id, o.outcome, o.routed, o.reasons,
       CAST(o.silent_error AS int) AS silent_error, o.confidence, CAST(o.scanned AS int) AS scanned,
       CASE WHEN o.outcome = 'correct' THEN 1 ELSE 0 END AS is_correct,
       CASE WHEN o.outcome = 'wrong' THEN 1 ELSE 0 END AS is_wrong,
       CASE WHEN o.outcome = 'missing' THEN 1 ELSE 0 END AS is_missing,
       CASE WHEN o.routed = 'review' THEN 1 ELSE 0 END AS is_review
FROM eval.field_outcome o JOIN desk.bank b ON b.bank_id = o.bank_id;
GO

CREATE OR ALTER VIEW dash.v_queue AS
SELECT i.item_id, b.name AS bank, l.loan_number, i.field_or_flag AS field, i.reason, i.priority, i.status,
       e.raw_value AS extracted, e.confidence, i.created_at, i.sla_due,
       DATEDIFF(hour, i.created_at, COALESCE(d.decided_at, sysutcdatetime())) AS age_hours,
       CASE WHEN COALESCE(d.decided_at, sysutcdatetime()) > i.sla_due THEN 1 ELSE 0 END AS breached,
       d.decision, d.new_value, d.reviewer, d.decided_at
FROM desk.review_item i
JOIN desk.loan l ON l.loan_id = i.loan_id
JOIN desk.bank b ON b.bank_id = l.bank_id
OUTER APPLY (SELECT TOP 1 x.raw_value, x.confidence FROM desk.extraction x
             WHERE x.loan_id = i.loan_id AND x.field = i.field_or_flag ORDER BY x.run_at DESC) e
OUTER APPLY (SELECT TOP 1 * FROM desk.review_decision rd WHERE rd.item_id = i.item_id ORDER BY rd.decided_at DESC) d;
GO

CREATE OR ALTER VIEW dash.v_runs AS
SELECT r.run_id, CAST(r.started_at AS date) AS day, r.stage, r.status, r.model_id, COALESCE(r.pages, 0) AS pages,
       DATEDIFF(second, r.started_at, r.ended_at) AS seconds,
       (SELECT value FROM ops.stage_metric m WHERE m.run_id = r.run_id AND m.metric = 'di_errors') AS di_errors,
       (SELECT value FROM ops.stage_metric m WHERE m.run_id = r.run_id AND m.metric = 'items_out') AS items_out,
       CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END AS failed
FROM ops.run_log r;
GO

CREATE OR ALTER VIEW dash.v_cost AS
SELECT day, service, units, CAST(cost_usd AS float) AS cost_usd FROM ops.cost_daily;
GO
