// ========== Document Intelligence Module ========== //
param solutionName string
param solutionLocation string
param managedIdentityObjectId string
param keyVaultName string

var abbrs = loadJsonContent('./abbreviations.json')

var documentIntelligenceName = '${abbrs.ai.documentIntelligence}${solutionName}'

// Document Intelligence resource
resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: documentIntelligenceName
  location: solutionLocation
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: documentIntelligenceName
    restrictOutboundNetworkAccess: false
    allowedFqdnList: []
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    publicNetworkAccess: 'Enabled'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// Role assignment for managed identity to access Document Intelligence
resource documentIntelligenceRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(documentIntelligence.id, managedIdentityObjectId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: documentIntelligence
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908') // Cognitive Services User
    principalId: managedIdentityObjectId
    principalType: 'ServicePrincipal'
  }
}

// Store Document Intelligence endpoint and key in Key Vault
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource documentIntelligenceEndpointSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'AZURE-DOCUMENT-INTELLIGENCE-ENDPOINT'
  parent: keyVault
  properties: {
    value: documentIntelligence.properties.endpoint
  }
}

resource documentIntelligenceKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  name: 'AZURE-DOCUMENT-INTELLIGENCE-KEY'
  parent: keyVault
  properties: {
    value: documentIntelligence.listKeys().key1
  }
}

// Outputs
output documentIntelligenceName string = documentIntelligence.name
output documentIntelligenceEndpoint string = documentIntelligence.properties.endpoint
output documentIntelligenceId string = documentIntelligence.id
