-- Loan Desk Q&A web app (infra/ask.bicep): its managed identity reads the loan tape, review state and knowledge.
-- Read-only: SELECT on exactly the tables the agent's fixed queries and document search use. ${ASK_APP_NAME} comes
-- from .env (tools/run_sql.py fills it).
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '${ASK_APP_NAME}')
    CREATE USER [${ASK_APP_NAME}] FROM EXTERNAL PROVIDER;
GO
GRANT SELECT ON review.loan TO [${ASK_APP_NAME}];
GRANT SELECT ON review.loan_detail TO [${ASK_APP_NAME}];
GRANT SELECT ON review.item_priority TO [${ASK_APP_NAME}];
GRANT SELECT ON review.item TO [${ASK_APP_NAME}];
GRANT SELECT ON SCHEMA::kb TO [${ASK_APP_NAME}];
GO
