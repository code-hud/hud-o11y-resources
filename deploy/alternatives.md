# Deploying with existing resources (ECS Fargate)

Use these instead of the [one-stack deploy](README.md) when you already run the Observability Stack or have an
AMP workspace. Commands below run from the repository root.

## Next to your Observability Stack

If you already run the [Observability Stack](https://github.com/opensearch-project/observability-stack), both
one-stack templates (`deploy/aws/hud-ecs.yaml` and `deploy/aws/hud.yaml`) give Hud its own data resources next to
yours:
- an OpenSearch Serverless collection, an OSIS pipeline and an AMP workspace, the same as the Observability
  Stack's. Their names are `NamePrefix` (default `hud`) plus a suffix unique to the stack, e.g. `hud-1a2b3c4d`, so
  they can't clash with yours;
- the collector on ECS Fargate (`hud-ecs.yaml`) or its EKS role (`hud.yaml`), writing to them;
- the read-only [Hud access](README.md#hud-access) role.

Your collection, pipeline, workspace and UI app aren't changed, except as below.

```sh
aws cloudformation deploy --stack-name hud --template-file deploy/aws/hud-ecs.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  ClusterName=<cluster> VpcId=<vpc> SubnetIds=<subnet-a>,<subnet-b> AllowedCidr=<vpc cidr> \
  ExternalId=<external id from Hud> ExistingUiAppId=<your OpenSearch UI app id>
```

**Your OpenSearch UI app.** Set `ExistingUiAppId` to your app's id (`aws opensearch list-applications`)
to add Hud's collection and AMP workspace (as a PromQL data source) to it. The stack reads your app's data sources
and adds its two; it never removes yours. Deleting the stack, or changing `ExistingUiAppId`, removes only those
two. Setting it on a stack that created its own UI app deletes that app (and clearing it creates one). No
workspace is created in your app: to use Hud's data in one of your workspaces, associate the two data
sources with it in the workspace's settings. Anyone in your account can read Hud's collection
([Who can read the collection](README.md#who-can-read-the-collection)).

Then point your apps at the stack's `OtlpEndpoint` and `HudExporter` outputs, as in the
[one-stack deploy](README.md#ecs-fargate-hud-ecsyaml), and give Hud the `HudRoleArn`, `CollectionName` and
`CollectionEndpoint` outputs.

## Separate stacks

The collector stack creates an AMP workspace unless you pass one, and grants the collector only
`aps:RemoteWrite` on it. Without OSIS parameters, only metrics are sent.

`deploy/aws/ecs-fargate.yaml` runs the collector as a service in your existing ECS cluster and VPC, reachable at
`http://hud-collector.hud.internal:4318` (private DNS; set `DnsNamespace` to another name, e.g. `hud2.internal`,
for a second install in the same VPC, and copy the endpoint from the `OtlpEndpoint` output). Defaults: 1 vCPU,
4 GB, ARM64, 1 task.

**1. Observability Stack** (skip if you already have one). `deploy/aws/observability.yaml` creates the same
resources as the official serverless
[installer](https://github.com/opensearch-project/observability-stack/blob/main/aws/cli-installer/README.md):
OpenSearch Serverless, an OSIS pipeline (logs, traces and service map to OpenSearch, metrics to AMP; logs go to
`hud-investigations-v1` rather than `logs-otel-v1`), an AMP workspace, and the OpenSearch UI with an Observability workspace and AMP as a PromQL data source (~10 minutes):

```sh
aws cloudformation deploy --stack-name hud-obs --template-file deploy/aws/observability.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides Name=hud-obs
out() { aws cloudformation describe-stacks --stack-name hud-obs --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
AMP_WS=$(out AmpWorkspaceId) OSIS_LOGS=$(out OsisLogsUrl); out DashboardUrl
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fobservability.yaml&stackName=hud-obs) (template: `deploy/aws/observability.yaml`)

With an existing stack (from the installer or built some other way), use its AMP workspace id and the full
OTLP logs URL of its OSIS pipeline (the pipeline must accept OTLP logs). The installer names both after the
pipeline: `aws amp list-workspaces --alias <pipeline>` and `https://<IngestEndpointUrls[0]>/<pipeline>/v1/logs`.

**2. Collector.** Deploy into public subnets, as in the [one-stack deploy](README.md#ecs-fargate-hud-ecsyaml), or see
[Private subnets](#private-subnets). The task
pulls the public `ghcr.io/code-hud/hud-otel-collector` image; to use a copy in your account's ECR, pass `Image`.

```sh
aws cloudformation deploy --stack-name hud-collector --template-file deploy/aws/ecs-fargate.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  ClusterName=<cluster> VpcId=<vpc> SubnetIds=<subnet-a>,<subnet-b> AllowedCidr=<vpc cidr> \
  AmpWorkspaceId=$AMP_WS OsisLogsUrl=$OSIS_LOGS OsisPipelineName=hud-obs
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fecs-fargate.yaml&stackName=hud-collector) (template: `deploy/aws/ecs-fargate.yaml`)

**3. Your apps.** Set `HUD_OTLP_ENDPOINT` and `HUD_EXPORTER` in your apps to the stack's `OtlpEndpoint` and
`HudExporter` outputs. Metrics show up in the OpenSearch UI through its AMP data source (PromQL), and
investigations in its logs.

## Private subnets

All ECS templates assume public subnets (with a route to an internet gateway) and give the tasks public IPs. For
private subnets with a NAT gateway, pass `AssignPublicIp=DISABLED`; the tasks then reach GHCR, AMP and OSIS through
the NAT gateway. Without a NAT gateway they can't pull the image from GHCR or reach OSIS's public endpoint.

## Hud access on its own

`deploy/aws/hud-access.yaml` creates only the read-only role described in [Hud access](README.md#hud-access),
for an AMP workspace and, optionally, an OpenSearch Serverless collection you already have (empty
`CollectionName` shares metrics only). `CollectionName` also limits the role's `aoss:APIAccessAll` to that
collection. Use the collector stack's `AmpWorkspaceId` output, or the Observability
Stack's `AmpWorkspaceId` and `CollectionName` outputs:

```sh
aws cloudformation deploy --stack-name hud-access --template-file deploy/aws/hud-access.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  ExternalId=<external id from Hud> AmpWorkspaceId=$AMP_WS CollectionName=hud-obs
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fhud-access.yaml&stackName=hud-access) (template: `deploy/aws/hud-access.yaml`)

Delete the stack to revoke access.
