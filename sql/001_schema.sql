-- Receivership Loan File Desk: Azure SQL serving schema (docs/DATA_MODEL.md).
-- Schemas: desk = the loan record and review results; ops = monitoring tables for the operations report.
-- Corrections never overwrite: desk.loan_field is system-versioned (every change is kept in its history table).

IF SCHEMA_ID('desk') IS NULL EXEC('CREATE SCHEMA desk');
IF SCHEMA_ID('ops')  IS NULL EXEC('CREATE SCHEMA ops');
GO

CREATE TABLE desk.bank (
    bank_id        int IDENTITY PRIMARY KEY,
    fdic_cert      int NULL,                 -- the real failed bank this fictional portfolio is modeled on
    name           nvarchar(200) NOT NULL,   -- fictional bank name
    closing_date   date NULL,
    acquirer       nvarchar(200) NULL,
    layout_family  nvarchar(20) NULL,        -- which synthetic layout set (held-out evaluation)
    split          nvarchar(10) NOT NULL CONSTRAINT ck_bank_split CHECK (split IN ('train','val','test')),
    status         nvarchar(20) NOT NULL DEFAULT 'receiving'
                   CONSTRAINT ck_bank_status CHECK (status IN ('receiving','processing','in_review','approved','released'))
);

CREATE TABLE desk.loan_file (
    file_id        int IDENTITY PRIMARY KEY,
    bank_id        int NOT NULL REFERENCES desk.bank(bank_id),
    blob_path      nvarchar(400) NOT NULL,
    sha256         char(64) NOT NULL,        -- the original is evidence: hash recorded at intake
    pages          int NOT NULL,
    version        int NOT NULL DEFAULT 1,   -- replacement scans add a version; old files are kept
    received_at    datetime2 NOT NULL DEFAULT sysutcdatetime(),
    CONSTRAINT uq_loan_file UNIQUE (blob_path, version)
);

CREATE TABLE desk.document (
    doc_id         int IDENTITY PRIMARY KEY,
    file_id        int NOT NULL REFERENCES desk.loan_file(file_id),
    doc_type       nvarchar(40) NOT NULL,    -- schema/loan_fields.yaml document_types
    page_from      int NOT NULL,
    page_to        int NOT NULL,
    classifier_confidence decimal(5,4) NULL,
    source         nvarchar(10) NOT NULL DEFAULT 'model' CHECK (source IN ('model','reviewer'))
);

CREATE TABLE desk.loan (
    loan_id        int IDENTITY PRIMARY KEY,
    bank_id        int NOT NULL REFERENCES desk.bank(bank_id),
    loan_number    nvarchar(40) NOT NULL,
    CONSTRAINT uq_loan UNIQUE (bank_id, loan_number)
);

-- One row per model output: raw evidence of what DI (or a text model) returned
CREATE TABLE desk.extraction (
    extraction_id  bigint IDENTITY PRIMARY KEY,
    doc_id         int NOT NULL REFERENCES desk.document(doc_id),
    loan_id        int NULL REFERENCES desk.loan(loan_id),
    field          nvarchar(60) NOT NULL,
    raw_value      nvarchar(400) NULL,
    normalized_value nvarchar(400) NULL,
    confidence     decimal(5,4) NULL,
    page           int NULL,
    bbox           nvarchar(200) NULL,       -- polygon JSON for the review highlight
    model_id       nvarchar(100) NOT NULL,
    model_version  nvarchar(40) NULL,
    run_id         uniqueidentifier NOT NULL,
    run_at         datetime2 NOT NULL DEFAULT sysutcdatetime()
);

-- The current value of every loan-tape field; history kept automatically (who/when/why on each change)
CREATE TABLE desk.loan_field (
    loan_id        int NOT NULL REFERENCES desk.loan(loan_id),
    field          nvarchar(60) NOT NULL,
    value          nvarchar(400) NULL,
    source         nvarchar(20) NOT NULL CHECK (source IN ('model','reviewer','data_room')),
    extraction_id  bigint NULL REFERENCES desk.extraction(extraction_id),
    changed_by     nvarchar(200) NOT NULL,
    reason         nvarchar(400) NULL,
    valid_from     datetime2 GENERATED ALWAYS AS ROW START NOT NULL,
    valid_to       datetime2 GENERATED ALWAYS AS ROW END NOT NULL,
    PERIOD FOR SYSTEM_TIME (valid_from, valid_to),
    CONSTRAINT pk_loan_field PRIMARY KEY (loan_id, field)
) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = desk.loan_field_history));

CREATE TABLE desk.text_flag (
    flag_id        bigint IDENTITY PRIMARY KEY,
    doc_id         int NOT NULL REFERENCES desk.document(doc_id),
    loan_id        int NULL REFERENCES desk.loan(loan_id),
    flag           nvarchar(40) NOT NULL,    -- schema/loan_fields.yaml text_flags
    quote          nvarchar(1000) NOT NULL,
    page           int NULL,
    confidence     decimal(5,4) NULL,
    quote_verified bit NOT NULL,             -- the quote was found verbatim in the page text
    model_id       nvarchar(100) NOT NULL,
    status         nvarchar(12) NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed','dismissed','added'))
);

CREATE TABLE desk.review_item (
    item_id        bigint IDENTITY PRIMARY KEY,
    loan_id        int NOT NULL REFERENCES desk.loan(loan_id),
    field_or_flag  nvarchar(60) NOT NULL,
    reason         nvarchar(20) NOT NULL CHECK (reason IN ('low_confidence','rule_failure','mismatch','burn_in','text_flag')),
    priority       tinyint NOT NULL DEFAULT 3,
    status         nvarchar(12) NOT NULL DEFAULT 'open' CHECK (status IN ('open','assigned','decided')),
    created_at     datetime2 NOT NULL DEFAULT sysutcdatetime(),
    sla_due        datetime2 NULL
);

CREATE TABLE desk.review_decision (
    decision_id    bigint IDENTITY PRIMARY KEY,
    item_id        bigint NOT NULL REFERENCES desk.review_item(item_id),
    decision       nvarchar(20) NOT NULL CHECK (decision IN ('confirm','correct','not_on_document','reclassify','dismiss')),
    new_value      nvarchar(400) NULL,
    reviewer       nvarchar(200) NOT NULL,
    comment        nvarchar(1000) NULL,
    decided_at     datetime2 NOT NULL DEFAULT sysutcdatetime()
);

CREATE TABLE desk.audit_event (
    event_id       bigint IDENTITY PRIMARY KEY,
    at             datetime2 NOT NULL DEFAULT sysutcdatetime(),
    actor          nvarchar(200) NOT NULL,
    action         nvarchar(60) NOT NULL,
    entity         nvarchar(60) NOT NULL,
    entity_id      nvarchar(60) NOT NULL,
    before_json    nvarchar(max) NULL,
    after_json     nvarchar(max) NULL
);
GO

-- ---------------------------------------------------------------- operations monitoring (DASHBOARD.md)
CREATE TABLE ops.run_log (
    run_id         uniqueidentifier PRIMARY KEY,
    stage          nvarchar(20) NOT NULL,    -- intake/classify/extract/flag/validate/reconcile/route/release
    bank_id        int NULL,
    started_at     datetime2 NOT NULL,
    ended_at       datetime2 NULL,
    status         nvarchar(10) NOT NULL CHECK (status IN ('running','ok','failed')),
    error          nvarchar(2000) NULL,
    pages          int NULL,
    model_id       nvarchar(100) NULL
);

CREATE TABLE ops.stage_metric (
    run_id         uniqueidentifier NOT NULL REFERENCES ops.run_log(run_id),
    metric         nvarchar(40) NOT NULL,    -- latency_ms, items_in, items_out, di_errors, throttled
    value          float NOT NULL,
    CONSTRAINT pk_stage_metric PRIMARY KEY (run_id, metric)
);

CREATE TABLE ops.daily (
    day            date NOT NULL,
    source         nvarchar(30) NOT NULL,    -- pipeline / log_analytics / cost_export
    metric         nvarchar(60) NOT NULL,
    value          float NOT NULL,
    CONSTRAINT pk_ops_daily PRIMARY KEY (day, source, metric)
);

CREATE TABLE ops.cost_daily (
    day            date NOT NULL,
    service        nvarchar(30) NOT NULL,    -- docintel, llm, databricks, sql, storage
    units          float NOT NULL,
    cost_usd       decimal(10,4) NOT NULL,
    CONSTRAINT pk_cost_daily PRIMARY KEY (day, service)
);
GO

-- SLA view for the review-ops page and the escalation flow
CREATE VIEW ops.review_sla AS
SELECT i.item_id, i.loan_id, i.reason, i.created_at, i.sla_due, d.decided_at,
       CASE WHEN COALESCE(d.decided_at, sysutcdatetime()) > i.sla_due THEN 1 ELSE 0 END AS breached
FROM desk.review_item i
LEFT JOIN desk.review_decision d ON d.item_id = i.item_id;
GO
