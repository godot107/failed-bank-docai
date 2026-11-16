// Loan Desk Q&A: a Foundry agent over the loan tape, the loan files, FDIC deal terms and the FDIC Resolutions
// Handbook, behind a small signed-in web page. Deployed on top of main.bicep into the same resource group
// (infra/ask_up.sh). Everything here is pay-per-use and scales to zero: ~$0 while nobody asks.
//   Foundry project on the existing AI account (the agent lives here; the model is main.bicep's gpt-4.1-mini)
//   Function App (Flex Consumption, Python 3.12) serving the page + /api/ask, Entra sign-in in front
//   Storage for the app (deployment package, answer cache, per-user daily question count)
//   Application Insights on the existing Log Analytics workspace

param location string = resourceGroup().location
@description('Region of the Foundry account (main.bicep aiLocation)')
param aiLocation string = 'eastus2'
@description('Existing Foundry (AIServices) account from main.bicep; allowProjectManagement must be true')
param foundryName string
@description('Existing Log Analytics workspace from main.bicep')
param logWorkspaceName string
param sqlServerFqdn string
param sqlDatabase string
@description('Entra app registration for sign-in (infra/ask_auth.sh)')
param appClientId string
@description('Agent id (empty on the first deploy; src/agent.py create prints it)')
param agentId string = ''
@description('Entra object ID of the operator: creates and evaluates the agent')
param operatorObjectId string
param ownerTag string = 'owner'
param environmentName string = 'dev'
@description('Questions per signed-in user per day (cost cap)')
param dailyQuestions int = 40

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'failed-bank-docai'
  environment: environmentName
  owner: ownerTag
  'cost-center': 'personal'
  'managed-by': 'bicep'
  component: 'ask'
}
var roles = {
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  blobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  tableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

// ---------------------------------------------------------------- Foundry project (agent home)
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: 'loan-desk'
  location: aiLocation
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: { displayName: 'loan-desk', description: 'Loan Desk Q&A agent: loan tape, loan files, FDIC deal terms, Resolutions Handbook' }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logWorkspaceName
}

// ---------------------------------------------------------------- app
resource st 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stfbdapp${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: { allowSharedKeyAccess: false, allowBlobPublicAccess: false, minimumTlsVersion: 'TLS1_2', supportsHttpsTrafficOnly: true }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: st
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: false } }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for c in ['deployments', 'answercache']: {
  parent: blob
  name: c
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource usage 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'usage'
}

// answers are cached for a week at most (the cache key also changes whenever the data is reloaded)
resource cacheExpiry 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: st
  name: 'default'
  properties: {
    policy: { rules: [ {
      name: 'expire-answer-cache'
      enabled: true
      type: 'Lifecycle'
      definition: {
        filters: { blobTypes: [ 'blockBlob' ], prefixMatch: [ 'answercache/' ] }
        actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 7 } } }
      }
    } ] }
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-fbd-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id, RetentionInDays: 30 }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-fbd-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-fbd-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: { storage: { type: 'blobContainer', value: '${st.properties.primaryEndpoints.blob}deployments', authentication: { type: 'SystemAssignedIdentity' } } }
      scaleAndConcurrency: { maximumInstanceCount: 2, instanceMemoryMB: 2048, triggers: { http: { perInstanceConcurrency: 4 } } }
      runtime: { name: 'python', version: '3.12' }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: st.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
        { name: 'PYTHON_THREADPOOL_THREAD_COUNT', value: '4' }
        { name: 'AzureWebJobsDisableHomepage', value: 'true' }
        { name: 'FOUNDRY_PROJECT_ENDPOINT', value: 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}' }
        { name: 'SQL_SERVER', value: sqlServerFqdn }
        { name: 'SQL_DATABASE', value: sqlDatabase }
        { name: 'AGENT_ID', value: agentId }
        { name: 'APP_STORAGE_ACCOUNT', value: st.name }
        { name: 'ASK_CACHE', value: '1' }
        { name: 'ASK_GUARDRAILS', value: '1' }
        { name: 'OPENAI_ENDPOINT', value: 'https://${foundry.name}.openai.azure.com/' }   // Content Safety host (guardrails)
        { name: 'ASK_DAILY_QUESTIONS', value: string(dailyQuestions) }
        { name: 'ASK_LOG_SALT', value: uniqueString(subscription().id, resourceGroup().id, 'fbd-log-pseudonym') }
      ]
    }
  }
}

resource appNoFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

resource appNoScmBasic 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}

resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: { requireAuthentication: true, unauthenticatedClientAction: 'RedirectToLoginPage', redirectToProvider: 'azureactivedirectory' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: { clientId: appClientId, openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0' }
        validation: { allowedAudiences: [ 'api://${appClientId}', appClientId ] }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

// ---------------------------------------------------------------- roles (least privilege; SQL access is granted in SQL)
var aiRoles = [roles.azureAIUser, roles.cognitiveServicesOpenAIUser, roles.cognitiveServicesUser]   // last: Content Safety

resource appOnFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in aiRoles: {
  name: guid(foundry.id, app.id, r)
  scope: foundry
  properties: { principalId: app.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

resource operatorOnFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in [roles.azureAIUser, roles.cognitiveServicesUser]: {
  name: guid(foundry.id, operatorObjectId, r)
  scope: foundry
  properties: { principalId: operatorObjectId, principalType: 'User', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

resource projectModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, project.id, roles.cognitiveServicesOpenAIUser)
  scope: foundry
  properties: { principalId: project.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAIUser) }
}

resource appOnStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in [roles.blobDataOwner, roles.tableDataContributor]: {
  name: guid(st.id, app.id, r)
  scope: st
  properties: { principalId: app.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r) }
}]

output foundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output appName string = app.name
output appUrl string = 'https://${app.properties.defaultHostName}'
output appPrincipalName string = app.name
output appStorageAccount string = st.name
