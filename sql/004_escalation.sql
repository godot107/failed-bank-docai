-- SLA escalation, run hourly by the Power Automate flow "Review SLA escalation" (powerapps/flows/).
-- Past-SLA open items go to priority 0 (top of the queue) and the escalation is logged.
CREATE OR ALTER PROCEDURE desk.escalate_past_sla AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @hit TABLE (item_id BIGINT);
    UPDATE desk.review_item SET priority = 0
    OUTPUT INSERTED.item_id INTO @hit
    WHERE status <> 'decided' AND sla_due < SYSUTCDATETIME() AND priority <> 0;
    INSERT INTO desk.audit_event (actor, action, entity, entity_id, before_json, after_json)
    SELECT 'flow:review-sla-escalation', 'escalate_past_sla', 'desk.review_item', CONVERT(NVARCHAR(60), item_id), NULL, '{"priority":0}'
    FROM @hit;
    SELECT COUNT(*) AS escalated FROM @hit;
END
GO
GRANT EXECUTE ON desk.escalate_past_sla TO [${APP_USER}];
GO
