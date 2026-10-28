# Deploying to your AWS account

Commands below run from the repository root.

The collector runs in your AWS account and writes to your Amazon Managed Service for Prometheus (AMP).
Investigations (Hud's error forensics) are sent as OTLP logs to the
[Observability Stack](https://github.com/opensearch-project/observability-stack)'s OpenSearch Ingestion (OSIS)
pipeline, so they land in its OpenSearch.

Two modes:
- `merge` (default): function metrics (`code.function.*`) from all your app pods add into one series per
  collector task/pod and deployment. Per-pod resource attributes (`process.pid`, `k8s.pod.name`, ...) are dropped
  from them; the kept ones become labels: `deployment_environment_name`, `deployment_id`, `k8s_namespace_name`,
  `cloud_provider`, `cloud_region`, `telemetry_sdk_version` and `hud_identity_version` (each only when your apps
  set it). During a rollout the old and new deployments are separate series, so for totals use
  `sum without (deployment_id, telemetry_sdk_version) (...)`. Flow and process metrics stay one series per app pod
  and also carry `deployment_environment_name`.
- `convert`: one series per app pod for every metric, each labeled `deployment_environment_name`.

In both modes every metric gets `deployment_environment_name` when your apps set the `deployment.environment.name`
resource attribute; `target_info`, `job` and `instance` are unchanged.

Both take the deltas the Hud SDK sends; point your apps at the collector with `HUD_EXPORTER=otlp` and `HUD_OTLP_ENDPOINT`.

## One stack

Hud's onboarding gives you one "Launch in CloudFormation" button. It picks the template from your answer to
"Where do you run your services?" and prefills the `ExternalId` Hud generated for you; don't change it:

| Your services run on | Template | What runs the collector |
|---|---|---|
| ECS | `deploy/aws/hud-ecs.yaml` | the stack, on ECS Fargate in your cluster and VPC |
| EKS | `deploy/aws/hud.yaml` (`CollectorPlatform=EKS`) | you, with the Helm chart; the stack creates its IAM role |
| anything else | `deploy/aws/hud.yaml` (`CollectorPlatform=None`) | you, with your own credentials |

`hud-ecs.yaml` is `hud.yaml` plus the collector on ECS: only it asks for a VPC and subnets. Both create, in one
stack (~10 minutes):
- the Observability Stack's data: the same resources as the official serverless
  [installer](https://github.com/opensearch-project/observability-stack/blob/main/aws/cli-installer/README.md),
  i.e. OpenSearch Serverless, an OSIS pipeline (logs, traces and service map to OpenSearch, metrics to AMP) and an
  AMP workspace. One difference: logs (Hud's investigations) go to the `hud-investigations-v1` index rather than
  the installer's `logs-otel-v1`, whose field mappings other apps' logs can make reject them. Their names are `NamePrefix` (default `hud`) plus a suffix unique to the stack, e.g.
  `hud-1a2b3c4d`, so they can't clash with an Observability Stack you already run;
- an OpenSearch UI app with an Observability workspace and AMP as a PromQL data source, or, if you set
  `ExistingUiAppId`, Hud's collection and AMP workspace added to your app instead (see
  [Already running the Observability Stack?](#already-running-the-observability-stack));
- a read-only role Hud can assume to query that data (see [Hud access](#hud-access)).

For testing, `scripts/quick-create-link.sh` prints a new ExternalId and the links of both templates with it
prefilled (the bare links below leave it empty). The AMP workspace is kept if the stack is deleted, but not when
the install itself fails. The OSIS pipeline logs to `/aws/vendedlogs/OpenSearchIngestion/<name>/<stack id>` (14
days), which is also kept, so a pipeline that never becomes active leaves evidence. Turning those logs on needs
the deploying principal to have CloudWatch Logs delivery permissions (`logs:CreateLogDelivery`,
`logs:PutResourcePolicy`, `logs:DescribeResourcePolicies`, `logs:DescribeLogGroups`), which administrators have.

The templates' Rules (e.g. at least one subnet, subnets in the chosen VPC, `ClusterName` with EKS) are checked
when the stack is created, as the console's quick-create does: a bad value rolls the stack back at once with the
Rule's message. They aren't checked when a change set is created (`aws cloudformation deploy` uses change sets), so
there a bad value fails later, at the resource that uses it.

### ECS Fargate: `hud-ecs.yaml`

The collector runs as a service in your existing ECS cluster and VPC, reachable at
`http://hud-collector.hud.internal:4318` (private DNS), writing metrics to the stack's AMP workspace and
investigations to its pipeline. Defaults: 1 vCPU, 4 GB, ARM64, 1 task. A VPC can hold only one private DNS
namespace with a given name, so a second install in the same VPC needs a different `DnsNamespace` (e.g.
`hud2.internal`); copy the endpoint from the `OtlpEndpoint` output.

Deploy the collector into public subnets (with a route to an internet gateway). The tasks get public IPs
(`AssignPublicIp=ENABLED`, the default) to pull the image and reach AMP and OSIS, but the security group only
opens port 4318 only to `AllowedCidr` (your VPC CIDR; `0.0.0.0/0` is rejected), so nothing is reachable from the
internet. For private subnets with a NAT gateway, see [alternatives](alternatives.md#private-subnets). The task pulls the public
`ghcr.io/code-hud/hud-otel-collector` image; to use a copy in your account's ECR, pass `Image`.

```sh
aws cloudformation deploy --stack-name hud --template-file deploy/aws/hud-ecs.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  ClusterName=<cluster> VpcId=<vpc> SubnetIds=<subnet-a>,<subnet-b> AllowedCidr=<vpc cidr> \
  ExternalId=<external id from Hud>
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fhud-ecs.yaml&stackName=hud) (template: `deploy/aws/hud-ecs.yaml`)

**Your apps.** Set `HUD_OTLP_ENDPOINT` and `HUD_EXPORTER` in your apps to the stack's `OtlpEndpoint` and
`HudExporter` outputs. Metrics show up in the OpenSearch UI (`DashboardUrl` output) through its AMP data
source (PromQL), and investigations in its logs, for anyone in your account (see
[Who can read the collection](#who-can-read-the-collection)).

### EKS or your own collector: `hud.yaml`

With `CollectorPlatform=EKS` the stack also creates the collector's IAM role, linked to the `hud/hud-collector`
service account with EKS Pod Identity (default) or IRSA (`AuthMode=IRSA OidcProvider=<issuer without https://>`).
The EKS fields are grouped as "EKS only"; with `None`, leave `ClusterName` empty (a rule enforces it). Then install
the official chart with `deploy/helm/values.yaml`:

```sh
aws cloudformation deploy --stack-name hud --template-file deploy/aws/hud.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  CollectorPlatform=EKS ClusterName=<cluster> ExternalId=<external id from Hud>
helm install hud opentelemetry-collector --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
  -n hud --create-namespace -f deploy/helm/values.yaml \
  --set-string extraEnvs[0].value=<AmpRemoteWriteUrl output>,extraEnvs[1].value=<region>
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fhud.yaml&stackName=hud) (template: `deploy/aws/hud.yaml`)

Add `-f deploy/helm/values-convert.yaml` for `convert` mode. With IRSA, also set the service account's
`eks.amazonaws.com/role-arn` annotation to the `RoleArn` output. Your apps send to `http://hud-collector.hud:4318`.
On EKS only metrics are sent; investigations aren't sent to OpenSearch yet.

With `CollectorPlatform=None`, nothing is created for the collector. Point yours at the `AmpRemoteWriteUrl`
output (metrics, SigV4 service `aps`) and the `OtlpEndpoint` output plus `/v1/logs` (investigations, SigV4
service `osis`), and give it credentials with `aps:RemoteWrite` on the workspace and `osis:Ingest` on the pipeline.

### Already running the Observability Stack?

Both templates leave yours alone: Hud's collection, pipeline and AMP workspace are created next to yours, with
unique names. Set `ExistingUiAppId` to your OpenSearch UI app's id to add Hud's data to it instead of creating a
new app (details in [alternatives](alternatives.md#next-to-your-observability-stack)). For separate stacks, or an
AMP workspace you already have, see [Deploying with existing resources](alternatives.md).

`observability.yaml`, `hud-ecs.yaml` and `hud.yaml` are generated from the templates in `deploy/aws/parts/`,
`ecs-fargate.yaml`, `eks-iam.yaml` and `hud-access.yaml`: edit those, then run `scripts/unify.py`.

## EKS role on its own

`deploy/aws/eks-iam.yaml` creates only the collector's IAM role (and an AMP workspace unless you pass
`AmpWorkspaceId=<ws-...>`), for when the rest is already in place. Install the chart as above with its
`AmpRemoteWriteUrl` output:

```sh
aws cloudformation deploy --stack-name hud-collector-iam --template-file deploy/aws/eks-iam.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides ClusterName=<cluster>
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Feks-iam.yaml&stackName=hud-collector-iam) (template: `deploy/aws/eks-iam.yaml`)

To give Hud access, deploy `deploy/aws/hud-access.yaml` with the stack's `AmpWorkspaceId` output (see
[Hud access](#hud-access)).

## Hud access

Hud reads your data from its own AWS account (`736129612463`) by assuming a role in yours. Both one-stack templates
create it; for other setups, deploy `deploy/aws/hud-access.yaml` on its own
([alternatives](alternatives.md#hud-access-on-its-own)).

The role trusts Hud's account, and only with the `ExternalId` Hud generated for you (prefilled in your launch
link). It can only read:
- AMP: `aps:QueryMetrics`, `aps:GetSeries`, `aps:GetLabels` and `aps:GetMetricMetadata` on the stack's AMP workspace.
- OpenSearch Serverless: describe the collection and its indexes and read documents (investigations), through a
  data access policy on the stack's collection. Its IAM policy allows `aoss:APIAccessAll`, which OpenSearch
  Serverless requires for any data call, only for that collection (`aoss:collection` condition).

It can't write to AMP or to any collection. Give Hud the `HudRoleArn`, `CollectionName` and
`CollectionEndpoint` outputs. To revoke access, delete the stack (with the one-stack deploy this removes the
collector and Observability Stack too), or delete the role in IAM.

### Who can read the collection

The collection's data access policies allow:
- the OSIS pipeline role: write (the only writer by default);
- your account root: read (describe the collection and its indexes, read documents), so any IAM role or user in
  your account that also has `aoss:APIAccessAll` (and `aoss:DashboardsAccessAll` for the OpenSearch UI) in IAM can
  read the data. This covers the OpenSearch UI setup and your OpenSearch UI users. Index patterns, saved searches,
  visualizations and dashboards are stored in the OpenSearch UI app, not in the collection, so read access is enough
  for them;
- Hud's role: read;
- the IAM roles or users in `DataWritePrincipals` (optional, comma-separated ARNs, no spaces): write, i.e. create and
  update indexes and documents, as the pipeline can. Empty by default. Use it only for something that writes to
  the collection itself, such as index or document requests in Dev Tools.

Deployed an earlier version? Its policy gave your account root read and write. Updating the stack makes everyone
in your account read-only; what the OpenSearch UI shows doesn't change. If something of yours writes to the
collection, pass its role in `DataWritePrincipals` in the same update.
