-- Loan Desk Q&A knowledge (src/knowledge.py loads it; idempotent).
--   kb.passage  searchable text with its source: loan-file pages (synthetic, Document Intelligence Layout),
--               purchase-and-assumption agreement pages (real, FDIC) and the FDIC Resolutions Handbook (real)
--   kb.failure  every FDIC-insured bank failure since 1934 (FDIC failures data; open-bank assistance excluded)
--   kb.deal     the winning bid on each sampled 2009-2011 failure (FDIC bid summaries), as printed
-- Search uses SQL full-text (CONTAINSTABLE ranking), so no search service is needed.
IF SCHEMA_ID('kb') IS NULL EXEC('CREATE SCHEMA kb');
GO
IF OBJECT_ID('kb.passage') IS NULL
CREATE TABLE kb.passage (
    passage_id  int IDENTITY CONSTRAINT pk_kb_passage PRIMARY KEY,
    source      nvarchar(20)  NOT NULL CONSTRAINT ck_kb_passage_source CHECK (source IN ('loan_file', 'pa_agreement', 'handbook', 'fdic')),
    ref         nvarchar(40)  NOT NULL,   -- loan file key (B4-0001), FDIC cert, or 'resolutions-handbook'
    loan_id     int NULL,                 -- review.loan.loan_id for loan-file pages
    bank        nvarchar(120) NULL,
    doc_type    nvarchar(40)  NULL,       -- loan-file document type
    page        int NOT NULL,             -- page within the document (loan files: within the split document)
    title       nvarchar(200) NOT NULL,
    url         nvarchar(400) NULL,
    text        nvarchar(max) NOT NULL
);
GO
-- widen the source list on databases created before 'fdic' existed (the original CHECK had a system name)
DECLARE @ck sysname = (SELECT name FROM sys.check_constraints WHERE parent_object_id = OBJECT_ID('kb.passage')
                       AND definition NOT LIKE '%fdic%');
IF @ck IS NOT NULL
BEGIN
    EXEC('ALTER TABLE kb.passage DROP CONSTRAINT ' + @ck);
    ALTER TABLE kb.passage ADD CONSTRAINT ck_kb_passage_source
        CHECK (source IN ('loan_file', 'pa_agreement', 'handbook', 'fdic'));
END;
GO
IF OBJECT_ID('kb.failure') IS NULL
CREATE TABLE kb.failure (
    failure_id  int NOT NULL CONSTRAINT pk_kb_failure PRIMARY KEY,   -- FDIC failures dataset ID
    cert        int NULL,
    bank        nvarchar(120) NOT NULL,
    city        nvarchar(80)  NULL,
    state       nchar(2)      NULL,
    fail_date   date NOT NULL,
    fail_year   int  NOT NULL,
    resolution  nvarchar(20)  NULL,       -- PA (purchase and assumption), PI, PO (payout), IDT, ...
    acquirer    nvarchar(200) NULL,
    assets_k    bigint NULL,              -- total assets, $ thousands
    deposits_k  bigint NULL,              -- total deposits, $ thousands
    cost_k      bigint NULL               -- estimated loss to the insurance fund, $ thousands
);
GO
IF OBJECT_ID('kb.deal') IS NULL
CREATE TABLE kb.deal (
    cert                 int NOT NULL CONSTRAINT pk_kb_deal PRIMARY KEY,
    bank                 nvarchar(120) NOT NULL,
    city                 nvarchar(80)  NULL,
    state                nchar(2)      NULL,
    fail_date            date NULL,
    buyer                nvarchar(200) NULL,
    transaction_type     nvarchar(120) NULL,
    deposit_premium      nvarchar(40)  NULL,   -- as printed in the bid summary, e.g. '0.00%'
    asset_discount_k     nvarchar(40)  NULL,   -- as printed, $ thousands, e.g. '$ (15,000)'
    sf_loss_share        nvarchar(40)  NULL,   -- single-family loss share, tranche 1, as printed ('N/A' = none)
    commercial_loss_share nvarchar(40) NULL,
    loss_share           bit NULL,
    bid_summary_url      nvarchar(400) NULL,
    pa_agreement_url     nvarchar(400) NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name = 'kb_ft')
    CREATE FULLTEXT CATALOG kb_ft;
GO
IF NOT EXISTS (SELECT 1 FROM sys.fulltext_indexes WHERE object_id = OBJECT_ID('kb.passage'))
    CREATE FULLTEXT INDEX ON kb.passage (text LANGUAGE 1033, title LANGUAGE 1033)
        KEY INDEX pk_kb_passage ON kb_ft WITH CHANGE_TRACKING AUTO;
GO
