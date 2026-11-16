// failed-bank-docai: Azure resources for Receivership Loan File Desk.
// Entra ID only: no storage keys, no API keys, no SQL passwords.
//   ADLS Gen2 (raw → di → pages → curated) · Key Vault (RBAC) · Document Intelligence · Azure SQL (free offer,
//   serving + ops tables) · Log Analytics + alerts · budgets · optional Foundry model for text flags
// Pattern adapted from the sibling projects (tx-fiscal-radar, insurance-docai). Deploy: infra/up.sh

param environmentName string = 'dev'
param location string = resourceGroup().location

@description('Document Intelligence region (eastus has the full prebuilt model set)')
param diLocation string = 'eastus'
@allowed(['F0', 'S0'])
@description('F0 = free (500 pages/month, first 2 pages per request); one F0 per subscription and kind')
param diSku string = 'F0'

@description('Entra object ID that gets data-plane roles and SQL admin (passed in by up.sh, never committed)')
param operatorObjectId string
param operatorLogin string = 'operator'
param ownerTag string = 'owner'
@description('IPs allowed through the SQL firewall (passed in by up.sh)')
param clientIps array = []
@description('Alert/budget email (passed in by up.sh)')
param alertEmail string

param budgetAmounts array = [25, 50, 100]
param budgetStartDate string = '2026-11-01'
param logDailyCapGb string = '0.1'

@description('Foundry model for text risk flags (U2). Off until phase 3.')
param deployAi bool = false
param aiLocation string = 'eastus2'
param chatModel string = 'gpt-4.1-mini'
param chatModelVersion string = '2025-04-14'
param chatCapacityK int = 20

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'failed-bank-docai'
  environment: environmentName
  owner: ownerTag
  'cost-center': 'personal'
  'managed-by': 'bicep'
}

var roles = {
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  blobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  kvSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
}

// ---------------------------------------------------------------- storage: ADLS Gen2
// raw = original loan files (immutable evidence) · docintel = raw DI JSON per run (container names need 3+ chars) · pages = page images for review
// curated = pipeline tables (Parquet) for Databricks · ops = run logs and metrics exports
resource lake 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stfbd${suffix}'
  location: location
  tags: union(tags, { component: 'lake' })
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    isHnsEnabled: true
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: lake
  name: 'default'
  properties: {
    // Corrections never overwrite: keep versions and deletes recoverable (audit trail, DATA_MODEL.md)
    isVersioningEnabled: false   // not supported with HNS; immutability is enforced by the pipeline (hash + write-once paths)
    deleteRetentionPolicy: { enabled: true, days: 7 }
    containerDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}

resource zones 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for z in ['raw', 'docintel', 'pages', 'curated', 'ops']: {
  parent: blobs
  name: z
  properties: { publicAccess: 'None' }
}]

// ---------------------------------------------------------------- training data (plain blob, no hierarchical namespace)
// The DI classifier build couldn't find training data on the ADLS Gen2 (HNS) lake ("TrainingContentMissing"), while
// neural model builds read the same container fine. Classifier training data lives here instead.
resource trainStore 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stfbdtr${suffix}'
  location: location
  tags: union(tags, { component: 'training' })
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    isHnsEnabled: false
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
  }
}

resource trainBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: trainStore
  name: 'default'
}

resource trainContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: trainBlobs
  name: 'training'
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------- Key Vault (RBAC mode)
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-fbd-${suffix}'
  location: location
  tags: union(tags, { component: 'secrets' })
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------- monitoring
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-fbd-${suffix}'
  location: location
  tags: union(tags, { component: 'monitoring' })
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json(logDailyCapGb) }
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-fbd-${environmentName}'
  location: 'global'
  tags: union(tags, { component: 'monitoring' })
  properties: {
    groupShortName: 'fbd'
    enabled: true
    emailReceivers: [ { name: 'operator', emailAddress: alertEmail, useCommonAlertSchema: true } ]
  }
}

// ---------------------------------------------------------------- Document Intelligence
resource docIntel 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'di-fbd-${suffix}'
  location: diLocation
  tags: union(tags, { component: 'docintel' })
  kind: 'FormRecognizer'
  sku: { name: diSku }
  identity: { type: 'SystemAssigned' }   // reads training data and loan files from the lake
  properties: {
    customSubDomainName: 'di-fbd-${suffix}'   // required for Entra ID auth
    disableLocalAuth: true                     // no API keys
    publicNetworkAccess: 'Enabled'
  }
}

resource diDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: docIntel
  name: 'to-log-analytics'
  properties: {
    workspaceId: logs.id
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

// Operations monitoring: email when DI returns server errors (throttling shows up as client errors on F0)
resource diErrors 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-fbd-di-errors'
  location: 'global'
  tags: union(tags, { component: 'monitoring' })
  properties: {
    severity: 2
    enabled: true
    scopes: [ docIntel.id ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [ {
        name: 'server-errors'
        criterionType: 'StaticThresholdCriterion'
        metricName: 'ServerErrors'
        metricNamespace: 'Microsoft.CognitiveServices/accounts'
        operator: 'GreaterThan'
        threshold: 0
        timeAggregation: 'Total'
      } ]
    }
    actions: [ { actionGroupId: actionGroup.id } ]
  }
}

// ---------------------------------------------------------------- Azure SQL (serving + ops tables, free offer)
resource sql 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-fbd-${suffix}'
  location: location
  tags: union(tags, { component: 'sql' })
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: operatorLogin
      sid: operatorObjectId
      tenantId: subscription().tenantId
      principalType: 'User'
    }
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sql
  name: 'loandesk'
  location: location
  tags: union(tags, { component: 'sql' })
  sku: { name: 'GP_S_Gen5_2', tier: 'GeneralPurpose', family: 'Gen5', capacity: 2 }
  properties: {
    useFreeLimit: true                         // free offer: 100k vCore-seconds + 32 GB per month
    // Bill past the free allowance (from the trial credit) instead of pausing for the rest of the month: a demo
    // that can't load is worse than a few dollars. Changed 2026-11-12 after scheduled flows nearly used the month's
    // allowance in two days (they're now off between demos). Pause sooner when idle to spend less of it.
    freeLimitExhaustionBehavior: 'BillOverUsage'
    autoPauseDelay: 15
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource sqlAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sql
  name: 'AllowAllWindowsAzureIps'   // Power BI, Power Apps connectors and Azure-hosted workers
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource sqlClients 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = [for (ip, i) in clientIps: {
  parent: sql
  name: 'client-${i}'
  properties: { startIpAddress: ip, endIpAddress: ip }
}]

// ---------------------------------------------------------------- optional: Foundry model for text flags (U2)
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (deployAi) {
  name: 'ai-fbd-${suffix}'
  location: aiLocation
  tags: union(tags, { component: 'foundry' })
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: 'ai-fbd-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true   // Loan Desk Q&A project (infra/ask.bicep)
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (deployAi) {
  parent: foundry
  name: chatModel
  sku: { name: 'GlobalStandard', capacity: chatCapacityK }
  properties: { model: { format: 'OpenAI', name: chatModel, version: chatModelVersion } }
}

// ---------------------------------------------------------------- role assignments (least privilege)
resource opLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lake.id, operatorObjectId, roles.blobDataContributor)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource diLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(lake.id, docIntel.id, roles.blobDataReader)
  scope: lake
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataReader)
    principalId: docIntel.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource diTrain 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(trainStore.id, docIntel.id, roles.blobDataReader)
  scope: trainStore
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataReader)
    principalId: docIntel.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource opTrain 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(trainStore.id, operatorObjectId, roles.blobDataContributor)
  scope: trainStore
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opDi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntel.id, operatorObjectId, roles.cognitiveServicesUser)
  scope: docIntel
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, operatorObjectId, roles.kvSecretsOfficer)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsOfficer)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource opAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAi) {
  name: guid(resourceGroup().id, 'ai-fbd', operatorObjectId, roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser)
    principalId: operatorObjectId
    principalType: 'User'
  }
}

// ---------------------------------------------------------------- budgets (RG scope, email at 100% actual + forecast)
resource budgets 'Microsoft.Consumption/budgets@2023-11-01' = [for amt in budgetAmounts: {
  name: 'failed-bank-docai-${amt}'
  properties: {
    category: 'Cost'
    amount: amt
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual100: { enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 100, thresholdType: 'Actual', contactEmails: [ alertEmail ] }
      forecast100: { enabled: true, operator: 'GreaterThanOrEqualTo', threshold: 100, thresholdType: 'Forecasted', contactEmails: [ alertEmail ] }
    }
  }
}]

output lakeAccount string = lake.name
output lakeDfsEndpoint string = lake.properties.primaryEndpoints.dfs
output lakeBlobEndpoint string = lake.properties.primaryEndpoints.blob
output trainBlobEndpoint string = trainStore.properties.primaryEndpoints.blob
output keyVaultUri string = kv.properties.vaultUri
output docIntelEndpoint string = docIntel.properties.endpoint
output docIntelSku string = diSku
output sqlServer string = sql.properties.fullyQualifiedDomainName
output sqlDatabase string = sqlDb.name
output logWorkspaceId string = logs.properties.customerId
output openaiEndpoint string = deployAi ? 'https://${foundry.name}.openai.azure.com/' : ''
