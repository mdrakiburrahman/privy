targetScope = 'subscription'

param resourceGroupName string = 'privy'
param resourceGroupLocation string = 'canadacentral'
param location string = 'canadacentral'
param serverName string = 'privy-sql'
param databaseName string = 'privy'
param administratorObjectId string
param administratorLogin string
param tenantId string

@allowed(['S0', 'S1', 'S2', 'S3', 'HS_Gen5_8', 'HS_MOPRMS_16', 'BC_Gen5_16'])
param databaseSku string = 'HS_Gen5_8'

resource group 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: resourceGroupLocation
}

module sql './sql.bicep' = {
  name: 'privy-sql'
  scope: group
  params: {
    location: location
    serverName: serverName
    databaseName: databaseName
    administratorObjectId: administratorObjectId
    administratorLogin: administratorLogin
    tenantId: tenantId
    databaseSku: databaseSku
  }
}

output serverFqdn string = sql.outputs.serverFqdn
output serverResourceId string = sql.outputs.serverResourceId
output databaseResourceId string = sql.outputs.databaseResourceId
output databaseName string = databaseName
output databaseSku string = databaseSku
