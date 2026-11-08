# Privy Azure SQL infrastructure

Deploys an Entra-only logical SQL server, `privy` (Standard S0, 10 DTUs), and an isolated
`privy_test` (Basic, 5 DTUs) in Canada Central. TLS 1.2 is required; transparent data encryption is
enabled. The single `privy-allow-all` firewall rule allows `0.0.0.0–255.255.255.255`, as explicitly
chosen for multi-machine access. The server is internet-reachable, but connections still require
Entra authentication and TLS. There are no passwords or Azure-services bypass rules. The application
bootstraps its schema using `AzureCliCredential`.

Prerequisites: PowerShell, an authenticated Azure CLI user with permission to deploy resources in
subscription `ce859648-30e1-4135-9d0f-8358aebfe789`, Bicep CLI, and (for application connectivity)
ODBC Driver 18 for SQL Server. The script derives the exact user's object ID and tenant from the
Azure CLI management token in memory. It never prints or writes that token.

```powershell
.\scripts\deploy_infra.ps1 -ValidateOnly
.\scripts\deploy_infra.ps1 `
  -SettingsPath "C:\isolated-privy\production\appsettings.yaml" `
  -TestSettingsPath "C:\isolated-privy\test\appsettings.yaml"
```

Replace the example paths with explicit isolated paths. Settings output is optional, uses the
application's version 1 YAML schema, and refuses overwrites, the primary checkout, the running hub
home, and the default hub home. Never commit real settings. Both test hub processes must use
`privy_test` with distinct isolated homes, ports, and the explicit test settings path. Do not change
the running hub or its settings.

Second machines need no IP allowlisting. The script no longer accepts client IPs and always deploys
the same allow-all rule. Deployments are incremental: omitted rules and unrelated resources are
preserved. Remove legacy per-IP rules explicitly after the first allow-all deployment; reruns do not
recreate them. The script rejects automatic changes to existing target server identity,
authentication, location, TLS/network settings or database SKU/backup/collation. Use a reviewed
Azure operation for intentional changes, then rerun.

The default server name is `privy-sql`; override `-ServerName` if needed for global uniqueness.
Database names and supported starting SKUs are parameters. All Azure operations pass the explicit
subscription; the script does not run `az account set`. Deployment outputs are nonsecret resource
IDs, FQDN, tiers, the firewall range, and explicit settings locations.

Canada Central S0 and Basic availability was checked against Azure capabilities on 2026-11-07. The
Azure Retail Prices API listed S0 at **USD 0.532/day** (about **USD 15.96 per 30 days**) before tax,
extra storage/backup, or agreement discounts. This is a retail estimate, not the subscription's
invoice price. Consult
[Azure SQL pricing](https://azure.microsoft.com/pricing/details/azure-sql-database/single/) for
current pricing; no cost limit or autoscaling is implied.

Run `pwsh -File .\infra\test_deploy.ps1` for isolated script tests using a mocked Azure CLI (input
guards, native exit-code failures, existing-server safety, validation-only behavior, YAML shape and
explicit subscription). Compile with `az bicep build --file .\infra\main.bicep --stdout`. Neither
command deploys.
