param location string
param serverName string
param databaseName string
param testDatabaseName string
param administratorObjectId string
param administratorLogin string
param tenantId string
param databaseSku string
param testDatabaseSku string
param clientIPv4Addresses array

resource server 'Microsoft.Sql/servers@2023-08-01' = {
  name: serverName
  location: location
  properties: {
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: 'User'
      login: administratorLogin
      sid: administratorObjectId
      tenantId: tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource database 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: server
  name: databaseName
  location: location
  sku: {
    name: databaseSku
    tier: 'Standard'
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource testDatabase 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: server
  name: testDatabaseName
  location: location
  sku: {
    name: testDatabaseSku
    tier: testDatabaseSku == 'Basic' ? 'Basic' : 'Standard'
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource databaseEncryption 'Microsoft.Sql/servers/databases/transparentDataEncryption@2023-08-01' = {
  parent: database
  name: 'current'
  properties: {
    state: 'Enabled'
  }
}

resource testDatabaseEncryption 'Microsoft.Sql/servers/databases/transparentDataEncryption@2023-08-01' = {
  parent: testDatabase
  name: 'current'
  properties: {
    state: 'Enabled'
  }
}

resource firewall 'Microsoft.Sql/servers/firewallRules@2023-08-01' = [for ip in clientIPv4Addresses: {
  parent: server
  name: 'privy-client-${replace(ip, '.', '-')}'
  properties: {
    startIpAddress: ip
    endIpAddress: ip
  }
}]

output serverFqdn string = server.properties.fullyQualifiedDomainName
output serverResourceId string = server.id
output databaseResourceId string = database.id
output testDatabaseResourceId string = testDatabase.id
