# Deploying to your AWS account

CloudFormation templates and Helm values for the [Hud OpenTelemetry collector](https://github.com/code-hud/hud-otel-collector) and the Observability Stack.

| Path | Contents |
| --- | --- |
| `deploy/aws/` | CloudFormation templates. `parts/` holds the sources; `observability.yaml`, `hud-ecs.yaml`, and `hud.yaml` are generated from them. |
| `deploy/helm/` | Helm values for the collector chart: `values.yaml`, and `values-convert.yaml` for convert mode. |
| `deploy/scripts/unify.py` | Generates the one-stack templates from `deploy/aws/parts/` and the separate templates. |
| `deploy/scripts/publish-templates.sh` | Publishes every template for a release to the S3 bucket the launch links use. |
| `deploy/scripts/collector-configs.py` | Renders every collector config the templates and Helm values can produce, and checks it. |
| `deploy/scripts/quick-create-link.sh` | Prints a new ExternalId and "Launch in CloudFormation" links with it prefilled. |
| [`deploy/alternatives.md`](alternatives.md) | Deploys that use an Observability Stack or AMP workspace you already have. |

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

For testing, `deploy/scripts/quick-create-link.sh` prints a new ExternalId and the links of both templates with it
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

`hud-ecs.yaml` is larger than the 51,200 bytes CloudFormation accepts inline, so the CLI uploads it to an S3 bucket
of yours in the stack's region (`--s3-bucket`).

```sh
aws cloudformation deploy --stack-name hud --template-file deploy/aws/hud-ecs.yaml \
  --s3-bucket <bucket> --capabilities CAPABILITY_IAM --parameter-overrides \
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
Pod Identity needs the `eks-pod-identity-agent` add-on on your cluster (built into EKS Auto Mode):

```sh
aws eks create-addon --cluster-name <cluster> --addon-name eks-pod-identity-agent
```

The EKS fields are grouped as "EKS only"; fill in `ClusterName` (the launch link leaves it empty). With `None`,
leave it empty (a rule enforces it). The role can write metrics to the stack's AMP workspace (`aps:RemoteWrite`) and
investigations to its pipeline (`osis:Ingest`). Then install the official chart with `deploy/helm/values.yaml`,
passing the stack's `AmpRemoteWriteUrl` and `OsisLogsUrl` outputs:

```sh
aws cloudformation deploy --stack-name hud --template-file deploy/aws/hud.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  CollectorPlatform=EKS ClusterName=<cluster> ExternalId=<external id from Hud>
helm install hud opentelemetry-collector --repo https://open-telemetry.github.io/opentelemetry-helm-charts \
  -n hud --create-namespace -f deploy/helm/values.yaml \
  --set-string 'extraEnvs[0].value=<AmpRemoteWriteUrl output>,extraEnvs[1].value=<region>,extraEnvs[2].value=<OsisLogsUrl output>'
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fhud.yaml&stackName=hud) (template: `deploy/aws/hud.yaml`)

Keep the quotes around `--set-string`'s value: zsh, the macOS default shell, otherwise reads `[0]` as a file
pattern. Add `-f deploy/helm/values-convert.yaml` for `convert` mode. With IRSA, also set the service account's
`eks.amazonaws.com/role-arn` annotation to the `RoleArn` output.

**Your apps.** Set `HUD_EXPORTER=otlp` and `HUD_OTLP_ENDPOINT=http://hud-collector.hud:4318` in your apps, next to
your usual `HUD_KEY` and `HUD_SERVICE` (tested with `hud-sdk` 1.12.1). The SDK still checks the key with Hud when it
starts, and doesn't load without it (E1004). Hud's settings for your key decide whether it sends investigations; set
`HUD_ENABLE_INVESTIGATION=true` to always send them. Metrics show up in the
OpenSearch UI (`DashboardUrl` output) through its AMP data source (PromQL), and investigations in its logs. To add
the Hud dashboards, see [Hud dashboards](#hud-dashboards).

With `CollectorPlatform=None`, nothing is created for the collector. Point yours at the `AmpRemoteWriteUrl`
output (metrics, SigV4 service `aps`) and the `OtlpEndpoint` output plus `/v1/logs` (investigations, SigV4
service `osis`), and give it credentials with `aps:RemoteWrite` on the workspace and `osis:Ingest` on the pipeline.

### Already running the Observability Stack?

Both templates leave yours alone: Hud's collection, pipeline and AMP workspace are created next to yours, with
unique names. Set `ExistingUiAppId` to your OpenSearch UI app's id to add Hud's data to it instead of creating a
new app (details in [alternatives](alternatives.md#next-to-your-observability-stack)). For separate stacks, or an
AMP workspace you already have, see [Deploying with existing resources](alternatives.md).

`observability.yaml`, `hud-ecs.yaml` and `hud.yaml` are generated from the templates in `deploy/aws/parts/`,
`ecs-fargate.yaml` and `eks-iam.yaml`: edit those, then run `deploy/scripts/unify.py`. They install
`hud-access.yaml` and `hud-hourly-rules.yaml` as nested stacks (see [Publishing](#publishing)).

## EKS role on its own

`deploy/aws/eks-iam.yaml` creates only the collector's IAM role (and an AMP workspace unless you pass
`AmpWorkspaceId=<ws-...>`), for when the rest is already in place. To send investigations, pass your pipeline's
logs URL and name (`OsisLogsUrl` and `OsisPipelineName`; the role then gets `osis:Ingest` on it). Install the chart
as above with its `AmpRemoteWriteUrl` and `OsisLogsUrl` outputs:

```sh
aws cloudformation deploy --stack-name hud-collector-iam --template-file deploy/aws/eks-iam.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides ClusterName=<cluster> \
  OsisLogsUrl=https://<pipeline endpoint>/<pipeline name>/v1/logs OsisPipelineName=<pipeline name>
```

The Helm values always send investigations, so they need a pipeline.

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Feks-iam.yaml&stackName=hud-collector-iam) (template: `deploy/aws/eks-iam.yaml`)

To give Hud access, deploy `deploy/aws/hud-access.yaml` with the stack's `AmpWorkspaceId` output (see
[Hud access](#hud-access)).

## Hud access

Hud reads your data from its own AWS account (`736129612463`) by assuming a role in yours. Both one-stack templates
create it, by installing `deploy/aws/hud-access.yaml` as a nested stack (`HudAccess`); for other setups, deploy `deploy/aws/hud-access.yaml` on its own
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

## Hud dashboards

The stack sets up the OpenSearch UI workspace and its data sources, but doesn't import the Hud dashboards. Import
them as in [`opensearch-ui/README.md`](../opensearch-ui/README.md#install-the-dashboards), skipping its step 1. For
step 2, use these values:
- `--workspace-id`: the part after `/w/` in the `DashboardUrl` output;
- `--opensearch-data-source-id`: in the workspace, **Data sources**, the one named after the `CollectionName` output;
- `--prometheus-connection` and `--prometheus-arn`: `<CollectionName with - replaced by _>_prometheus`, e.g.
  `hud_1a2b3c4d_prometheus`, and `arn:aws:opensearch:<region>:<account>:datasource/<that name>`;
- `--logs-index-pattern 'hud-investigations-v1*' --logs-layout otel`: where and how the stack's pipeline stores
  investigations.

The metrics panels work as soon as your apps send data; the week-over-week ones need a week of it. The
investigation panels don't work on the stack's OpenSearch Serverless collection yet: they use PPL functions
(`json_extract`, `spath`) that Serverless doesn't support, so they show a query error.

## Hourly recording rules

`deploy/aws/hud-hourly-rules.yaml` creates the `hud-hourly` rule namespace in an AMP workspace: recording rules
that store each service's, flow's, function's and instance's increase over the last hour, so the Hud dashboards
can sum hourly points instead of scanning raw samples. It is the only place these rules live.

The rules evaluate every 5 minutes; every sample is the full trailing-hour increase, so the interval only sets
freshness and cost, not accuracy. The dashboards read each record through `last_over_time(<record>[10m])` so that a
5-minute gap between samples never drops an hour boundary. Keep the two in step if you change either.

Every template that creates an AMP workspace installs it as a nested stack, `HourlyRecordingRules`, from
`<TemplatesUrl>/hud-hourly-rules.yaml`: the one-stack templates, `observability.yaml`, and `ecs-fargate.yaml` and
`eks-iam.yaml` when you leave `AmpWorkspaceId` empty. `TemplatesUrl` defaults to the folder the template was
published in (see [Publishing](#publishing)); change it only to test your own copy. Deleting the stack deletes the rules (the workspace is kept).

For a workspace you bring (`AmpWorkspaceId=<ws-...>`), the templates leave it alone, since it may already have the
rules. To add them, deploy the template on its own:

```sh
aws cloudformation deploy --stack-name hud-hourly-rules --template-file deploy/aws/hud-hourly-rules.yaml \
  --parameter-overrides AmpWorkspaceId=<ws-...>
```

[Launch in CloudFormation](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=https%3A%2F%2Fhud-cloudformation-templates.s3.amazonaws.com%2Ftemplates%2Fhud-hourly-rules.yaml&stackName=hud-hourly-rules) (template: `deploy/aws/hud-hourly-rules.yaml`)

## Development

`observability.yaml`, `hud-ecs.yaml` and `hud.yaml` in `deploy/aws/` are generated: edit the sources and run `deploy/scripts/unify.py` (needs PyYAML). The `cfn-lint` workflow runs `deploy/scripts/unify.py --check` and cfn-lint 1.58.0 on every template, and `deploy/scripts/collector-configs.py --validate`: it renders the collector config of `ecs-fargate.yaml` (merge and convert, with and without logs) and of the Helm values (merge and convert, both with logs), checks their pipelines, and runs `validate` on each in the collector image they pin (needs Docker; without `--validate` it only renders and checks). 
### Publishing

The templates are published to `https://hud-cloudformation-templates.s3.amazonaws.com/templates/`, one folder per
release (`templates/<version>/`), plus the latest release at `templates/` itself, which the launch links in these
docs use. The version is the release tag. Pushing a tag runs the `publish-templates` workflow, which runs cfn-lint
and `deploy/scripts/publish-templates.sh <tag>` (you can also run the workflow by hand with a tag). It needs the
`TEMPLATES_PUBLISH_ROLE_ARN` repository variable; see the workflow file.

`publish-templates.sh` (needs PyYAML) checks that the generated templates are up to date, then uploads every `deploy/aws/*.yaml` to
`templates/<version>/` and then to `templates/`. In each copy it sets the `TemplatesUrl` default to
`templates/<version>`, so a parent always nests the `hud-access.yaml` and `hud-hourly-rules.yaml` published with it,
even when you launch it from `templates/`. It doesn't overwrite a published version unless you set `FORCE=1`, and
`LATEST=0` leaves `templates/` alone. `--dryrun` only renders the files (into `OUT`, a temp dir by default) and
prints the uploads:

```sh
deploy/scripts/publish-templates.sh --dryrun 1.2.3
```

`VERSION=<tag> deploy/scripts/quick-create-link.sh` prints launch links for a published release. In the repository
the `TemplatesUrl` default is `templates/`, so a stack you deploy from a checkout nests the latest release's children.

To test a change to `hud-hourly-rules.yaml`, check its rules with promtool:

```sh
python3 -c 'import sys; sys.path.insert(0, "deploy/scripts"); import unify; print(unify.load("hud-hourly-rules.yaml")["Resources"]["HourlyRecordingRules"]["Properties"]["Data"], end="")' >/tmp/hud-hourly.rules.yaml
promtool check rules /tmp/hud-hourly.rules.yaml
```

Then deploy it on its own to a test workspace (see [Hourly recording rules](#hourly-recording-rules)). To test a
change to any nested template in place, publish to a bucket of yours (`BUCKET=<bucket> LATEST=0
deploy/scripts/publish-templates.sh test-1`) and launch from it
(`BASE_URL=https://<bucket>.s3.amazonaws.com/templates/test-1 deploy/scripts/quick-create-link.sh`).

The `collector-bump` workflow opens a PR that points the templates and Helm values at a new `ghcr.io/code-hud/hud-otel-collector` tag. hud-otel-collector's release workflow triggers it; you can also run it by hand with the tag.
