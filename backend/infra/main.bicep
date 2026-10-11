// Core Azure resources for the PS02 voice appointment agent (see docs/azure_hosting.md).
//
// Two-step deployment, because the image must exist in ACR before the app can pull it:
//   1. az deployment group create -g <rg> -f infra/main.bicep -p namePrefix=voiceagent
//   2. az acr build -r <acrName output> -t voice-agent:1.0 .
//      az deployment group create -g <rg> -f infra/main.bicep -p namePrefix=voiceagent \
//         deployApp=true containerImage=<acrLoginServer output>/voice-agent:1.0
//
// Azure SQL, Front Door and private endpoints are part of the design document but not this template.

targetScope = 'resourceGroup'

@description('Prefix for resource names: lowercase letters and digits, 3-11 characters.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'voiceagent'

@description('Region. Central India keeps speech and customer data in India.')
param location string = resourceGroup().location

@description('Create the Container App. Set to true after the image has been pushed to ACR.')
param deployApp bool = false

@description('Full image reference, e.g. <acr>.azurecr.io/voice-agent:1.0')
param containerImage string = ''

var suffix = take(uniqueString(resourceGroup().id), 6)
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
}

// ------------------------------------------------------------------ observability
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appi'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ------------------------------------------------------------------------- speech
resource speech 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: '${namePrefix}-speech-${suffix}'
  location: location
  kind: 'SpeechServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${namePrefix}-speech-${suffix}'
    publicNetworkAccess: 'Enabled'
  }
}

// ------------------------------------------------------------------- audio storage
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${namePrefix}st${suffix}'
  location: location
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource audioContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'audio'
  properties: { publicAccess: 'None' }
}

resource audioLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-audio-after-30-days'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ 'audio/' ]
            }
            actions: {
              baseBlob: { delete: { daysAfterModificationGreaterThan: 30 } }
            }
          }
        }
      ]
    }
  }
}

// ------------------------------------------------------------------------ secrets
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-kv-${suffix}'
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
  }
}

resource speechKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'speech-key'
  properties: { value: speech.listKeys().key1 }
}

// ------------------------------------------------------------ registry + identity
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${namePrefix}acr${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-api-id'
  location: location
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, appIdentity.id, roles.acrPull)
  scope: acr
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource kvSecretsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, roles.keyVaultSecretsUser)
  scope: keyVault
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

resource blobAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, appIdentity.id, roles.storageBlobDataContributor)
  scope: storage
  properties: {
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
  }
}

// ----------------------------------------------------------------- container apps
resource containerEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource api 'Microsoft.App/containerApps@2023-05-01' = if (deployApp) {
  name: '${namePrefix}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appIdentity.id}': {} }
  }
  dependsOn: [ acrPullAssignment, kvSecretsAssignment ]
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        { server: acr.properties.loginServer, identity: appIdentity.id }
      ]
      secrets: [
        { name: 'speech-key', keyVaultUrl: speechKeySecret.properties.secretUri, identity: appIdentity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'SPEECH_PROVIDER', value: 'azure' }
            { name: 'AZURE_SPEECH_KEY', secretRef: 'speech-key' }
            { name: 'AZURE_SPEECH_REGION', value: location }
            { name: 'AZURE_STT_LANGUAGE', value: 'en-IN' }
            { name: 'AZURE_TTS_VOICE', value: 'en-IN-NeerjaNeural' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      // One replica while the app stores data in container-local SQLite (pilot only).
      // After moving to Azure SQL (docs/azure_hosting.md §3), set minReplicas 2 / maxReplicas 10.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output speechEndpoint string = speech.properties.endpoint
output apiUrl string = deployApp ? 'https://${api.properties.configuration.ingress.fqdn}' : ''
