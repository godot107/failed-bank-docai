// dev environment. Deploy: infra/up.sh
using '../main.bicep'

param environmentName = 'dev'
param diSku = readEnvironmentVariable('DI_SKU', 'F0')
param budgetAmounts = [25, 50, 100]
param deployAi = bool(readEnvironmentVariable('DEPLOY_AI', 'false'))

// Identity- and person-specific values come from the deploying shell (never committed):
param operatorObjectId = readEnvironmentVariable('OPERATOR_OBJECT_ID')
param operatorLogin = readEnvironmentVariable('OPERATOR_LOGIN', 'operator')
param ownerTag = readEnvironmentVariable('OWNER_TAG', 'owner')
param alertEmail = readEnvironmentVariable('ALERT_EMAIL')
param clientIps = json(readEnvironmentVariable('CLIENT_IPS', '[]'))
