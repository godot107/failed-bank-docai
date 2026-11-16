-- Least-privilege access for the Power Apps review app (signed in as the work account, Entra ID).
-- The app can read the queue and the extracted values, record decisions, and write a corrected value (which the
-- temporal table versions automatically). It can't read the answer key (eval.*) or change anything else.
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '${APP_USER}')
    CREATE USER [${APP_USER}] FROM EXTERNAL PROVIDER;
GO
GRANT SELECT ON dash.v_queue TO [${APP_USER}];
GRANT SELECT ON dash.v_bank TO [${APP_USER}];
GRANT SELECT ON desk.review_item TO [${APP_USER}];
GRANT UPDATE (status) ON desk.review_item TO [${APP_USER}];
GRANT SELECT, INSERT ON desk.review_decision TO [${APP_USER}];
GRANT SELECT ON desk.extraction TO [${APP_USER}];
GRANT SELECT ON desk.document TO [${APP_USER}];
GRANT SELECT ON desk.loan TO [${APP_USER}];
GRANT SELECT ON desk.loan_file TO [${APP_USER}];
GRANT SELECT, UPDATE (value, source, changed_by, reason) ON desk.loan_field TO [${APP_USER}];
GRANT INSERT ON desk.audit_event TO [${APP_USER}];
DENY SELECT ON SCHEMA::eval TO [${APP_USER}];
GO
