targetScope = 'subscription'

param resourceGroupName string = 'privy'
param resourceGroupLocation string = 'canadacentral'
param location string = 'canadacentral'
param serverName string = 'privy-sql'
param databaseName string = 'privy'
param testDatabaseName string = 'privy_test'
param administratorObjectId string
param administratorLogin string
param tenantId string

@allowed(['S0', 'S1', 'S2', 'S3'])
param databaseSku string = 'S0'

@allowed(['Basic', 'S0'])
param testDatabaseSku string = 'Basic'

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
    testDatabaseName: testDatabaseName
    administratorObjectId: administratorObjectId
    administratorLogin: administratorLogin
    tenantId: tenantId
    databaseSku: databaseSku
    testDatabaseSku: testDatabaseSku
  }
}

output serverFqdn string = sql.outputs.serverFqdn
output serverResourceId string = sql.outputs.serverResourceId
output databaseResourceId string = sql.outputs.databaseResourceId
output testDatabaseResourceId string = sql.outputs.testDatabaseResourceId
output databaseName string = databaseName
output testDatabaseName string = testDatabaseName
output databaseSku string = databaseSku
output testDatabaseSku string = testDatabaseSku
