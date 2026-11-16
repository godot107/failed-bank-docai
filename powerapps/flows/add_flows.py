"""Add the Power Automate flows (flows/*.flow.json) and their SQL connection reference to an unpacked solution folder.

Each flow is a scheduled cloud flow that runs one stored procedure through the SQL Server connector (Entra auth,
the work account's connection). Solution-aware: the flows ship in the LoanFileDesk solution, bound at import time to
an existing connection through a deployment-settings file (no connection secrets in git).

Usage (called by deploy.sh): python3 flows/add_flows.py <solution folder> <sql server> <database>
"""
import json
import sys
import uuid
from pathlib import Path

REF = "fbd_sharedsql_loandesk"
FLOWS = {   # name → (procedure, recurrence)
    "Apply review decisions": ("[desk].[apply_review_decisions]", {"frequency": "Minute", "interval": 15}),
    "Review SLA escalation": ("[desk].[escalate_past_sla]", {"frequency": "Hour", "interval": 1}),
}


def flow_json(proc: str, recurrence: dict, server: str, database: str) -> dict:
    return {"properties": {
        "connectionReferences": {"shared_sql": {"runtimeSource": "embedded",
                                                "connection": {"connectionReferenceLogicalName": REF},
                                                "api": {"name": "shared_sql"}}},
        "definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                           "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
            "triggers": {"Recurrence": {"recurrence": recurrence, "type": "Recurrence"}},
            "actions": {"Run_procedure": {
                "runAfter": {}, "type": "OpenApiConnection",
                "inputs": {"host": {"connectionName": "shared_sql", "operationId": "ExecuteProcedure_V2",
                                    "apiId": "/providers/Microsoft.PowerApps/apis/shared_sql"},
                           "parameters": {"server": server, "database": database, "procedure": proc},
                           "authentication": "@parameters('$authentication')"}}}},
        "templateName": ""}, "schemaVersion": "1.0.0.0"}


def main() -> None:
    sol, server, database = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    (sol / "Workflows").mkdir(exist_ok=True)
    cust = (sol / "Other" / "Customizations.xml").read_text(encoding="utf-8-sig")
    manifest = (sol / "Other" / "Solution.xml").read_text(encoding="utf-8-sig")
    wf_xml, roots = [], []
    for name, (proc, rec) in FLOWS.items():
        wid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"failed-bank-docai/flow/{name}")).upper()   # stable across builds
        fname = f"{name.replace(' ', '')}-{wid}.json"
        (sol / "Workflows" / fname).write_text(json.dumps(flow_json(proc, rec, server, database), indent=1))
        wf_xml.append(f"""    <Workflow WorkflowId="{{{wid.lower()}}}" Name="{name}">
      <JsonFileName>/Workflows/{fname}</JsonFileName>
      <Type>1</Type><Subprocess>0</Subprocess><Category>5</Category><Mode>0</Mode><Scope>4</Scope>
      <OnDemand>0</OnDemand><TriggerOnCreate>0</TriggerOnCreate><TriggerOnDelete>0</TriggerOnDelete>
      <AsyncAutodelete>0</AsyncAutodelete><SyncWorkflowLogOnFailure>0</SyncWorkflowLogOnFailure>
      <StateCode>1</StateCode><StatusCode>2</StatusCode><RunAs>1</RunAs><IsTransacted>1</IsTransacted>
      <IntroducedVersion>1.0.0.0</IntroducedVersion><IsCustomizable>1</IsCustomizable><BusinessProcessType>0</BusinessProcessType>
      <IsCustomProcessingStepAllowedForOtherPublishers>1</IsCustomProcessingStepAllowedForOtherPublishers>
      <PrimaryEntity>none</PrimaryEntity>
      <LocalizedNames><LocalizedName languagecode="1033" description="{name}" /></LocalizedNames>
    </Workflow>""")
        roots.append(f'      <RootComponent type="29" id="{{{wid.lower()}}}" behavior="0" />')
    cust = cust.replace("<Workflows />", "<Workflows>\n" + "\n".join(wf_xml) + "\n  </Workflows>")
    ref = f"""  <connectionreferences>
    <connectionreference connectionreferencelogicalname="{REF}">
      <connectionreferencedisplayname>SQL Server (loan file desk)</connectionreferencedisplayname>
      <connectorid>/providers/Microsoft.PowerApps/apis/shared_sql</connectorid>
      <iscustomizable>1</iscustomizable><statecode>0</statecode><statuscode>1</statuscode>
    </connectionreference>
  </connectionreferences>
  <Languages>"""
    cust = cust.replace("  <Languages>", ref, 1)
    (sol / "Other" / "Customizations.xml").write_text(cust, encoding="utf-8")
    manifest = manifest.replace("</RootComponents>", "\n".join(roots) + "\n    </RootComponents>", 1)
    (sol / "Other" / "Solution.xml").write_text(manifest, encoding="utf-8")
    print(f"added {len(FLOWS)} flows + connection reference {REF}")


if __name__ == "__main__":
    main()
