# Privy Azure SQL infrastructure

Deploys an Entra-only logical SQL server and `privy` (Hyperscale `HS_Gen5_8`: Gen5, 8 vCores) in
Canada Central. TLS 1.2 is required; transparent data encryption is enabled. The single
`privy-allow-all` firewall rule allows `0.0.0.0–255.255.255.255`, as explicitly chosen for
multi-machine access. The server is internet-reachable, but connections still require Entra
authentication and TLS. There are no passwords or Azure-services bypass rules. The application
bootstraps its schema using `AzureCliCredential`.

Prerequisites: PowerShell, an authenticated Azure CLI user with permission to deploy resources in
subscription `ce859648-30e1-4135-9d0f-8358aebfe789`, Bicep CLI, and (for application connectivity)
ODBC Driver 18 for SQL Server. The script derives the exact user's object ID and tenant from the
Azure CLI management token in memory. It never prints or writes that token.

```powershell
.\scripts\deploy_infra.ps1 -ValidateOnly
.\scripts\deploy_infra.ps1 `
  -SettingsPath "C:\isolated-privy\production\appsettings.yaml"
```

Replace the example paths with explicit isolated paths. Settings output is optional, uses the
application's version 1 YAML schema, and refuses overwrites, the primary checkout, the running hub
home, and the default hub home. Never commit real settings. No test database or test settings are
provisioned. Do not change the running hub or its settings.

Second machines need no IP allowlisting. The script no longer accepts client IPs and always deploys
the same allow-all rule. Deployments are incremental: omitted rules and unrelated resources are
preserved. Remove legacy per-IP rules explicitly after the first allow-all deployment; reruns do not
recreate them. The script rejects automatic changes to existing target server identity,
authentication, location, TLS/network settings or database SKU/HA/read-scale/zone/backup/collation.
Use a reviewed Azure operation for intentional changes, then rerun.

Existing databases must report a requested service objective matching the selected SKU; the script
refuses to override pending SKU changes or missing requested-objective metadata. Hyperscale zone
redundancy must be explicitly reported as boolean `false`, not missing, null, or a string.

The default server name is `privy-sql`; override `-ServerName` if needed for global uniqueness. The
database name and supported SKUs are parameters. `HS_Gen5_8` is the default; `S0`, `S1`, `S2`, and
`S3` remain available for intentional Standard-tier deployments. The Hyperscale configuration uses
one high-availability replica, enables read scale-out, disables zone redundancy, and retains Local
backup storage redundancy. All Azure operations pass the explicit subscription; the script does not
run `az account set`. Deployment outputs are nonsecret resource IDs, FQDN, tiers, the firewall
range, and explicit settings locations.

Using an indicative rate of **USD 0.22 per vCore-hour**, `HS_Gen5_8` with one HA replica is
approximately **USD 84.48/day** for compute (`$0.22 × 8 vCores × 2 replicas × 24 hours`); storage,
backup, tax, and agreement discounts are additional. This is an indicative retail estimate, not a
quote or the subscription's invoice price. Actual rates vary by region and agreement. Consult
[Azure SQL pricing](https://azure.microsoft.com/pricing/details/azure-sql-database/single/) before
changing capacity; no cost limit or autoscaling is implied.

Run `pwsh -File .\infra\test_deploy.ps1` for isolated script tests using a mocked Azure CLI (input
guards, native exit-code failures, existing-server safety, validation-only behavior, YAML shape and
explicit subscription). Compile with `az bicep build --file .\infra\main.bicep --stdout`. Neither
command deploys.
