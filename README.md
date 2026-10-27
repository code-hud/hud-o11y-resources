# o11y-templates

Ready-made OpenSearch dashboards and Prometheus alert rules for data produced by the Hud SDK.

| Path | Contents |
| --- | --- |
| `dashboards/hud.ndjson` | The Hud dashboards ("Hud: ..."), together with their panels, queries and index pattern. Exported from OpenSearch UI with related objects included. |
| `rules/hud-alerts.yml` | Prometheus rule groups (Hud flow error alerts) for the Hud SDK metrics. |

Releases are tagged the same way as the Hud SDK releases. Use the tag that matches the SDK version you run.

## Download

```bash
VERSION=<release tag>   # e.g. the SDK version you run
curl -LO https://raw.githubusercontent.com/code-hud/o11y-templates/${VERSION}/dashboards/hud.ndjson
curl -LO https://raw.githubusercontent.com/code-hud/o11y-templates/${VERSION}/rules/hud-alerts.yml
```

## Prerequisites

- An OpenSearch UI application with a workspace for the Hud data, and permission to import saved objects into it.
- The Hud SDK metrics in Amazon Managed Service for Prometheus (AMP). Most panels are PromQL queries against them.
- Hud forensics exported as OTLP logs to an OpenSearch domain, in indices matching `logs-otel-v1*`. The investigations and exceptions panels are PPL queries against these logs.

## Install the dashboards

### 1. Connect your data sources

1. **OpenSearch domain.** In the AWS console, open **Amazon OpenSearch Service** > **OpenSearch UI (Dashboards)** > your application, and add the OpenSearch domain that holds the Hud logs. Its domain endpoint looks like `https://search-<domain-name>-<id>.<region>.es.amazonaws.com` (shown on the domain's page in the console). Then, in OpenSearch UI, open the workspace and associate the domain with it under **Data sources**.
2. **Prometheus.** In the same workspace, open **Data sources** > **Create data connection** > **Amazon Managed Service for Prometheus**, select the AMP workspace that receives the Hud SDK metrics, and name the connection `hud_amp`. The panels look the connection up by this name.

### 2. Fill in your IDs

`hud.ndjson` contains two placeholders that must be replaced before import:

| Placeholder | Replace with | Where to find it |
| --- | --- | --- |
| `HUD_WORKSPACE_ID` | Your OpenSearch UI workspace ID | The part after `/w/` in the workspace URL, e.g. `abc123` in `https://application-…opensearch.amazonaws.com/w/abc123/app/…` |
| `HUD_OPENSEARCH_DATA_SOURCE_ID` | The ID of the OpenSearch domain's data source from step 1 | Open **Data sources** and click the domain; the ID is the last part of the page URL |

```bash
sed -e 's/HUD_WORKSPACE_ID/<workspace id>/g' \
    -e 's/HUD_OPENSEARCH_DATA_SOURCE_ID/<data source id>/g' \
    hud.ndjson > hud-filled.ndjson
```

If you named the Prometheus connection something other than `hud_amp`, also add `-e 's/hud_amp/<connection name>/g'`.

### 3. Import

1. Open OpenSearch UI and switch to the workspace.
2. Open **Assets** (in OpenSearch UI without workspaces: **Dashboards Management** > **Saved objects**).
3. Click **Import** and select `hud-filled.ndjson`.
4. Choose **Check for existing objects** > **Automatically overwrite conflicts**, and click **Import**. Keep the original object IDs (do not choose **Create new objects with random IDs**): the "click one for its page" links between the dashboards refer to them.
5. Go to **Dashboards** and open any of the dashboards whose names start with `Hud:`. Pick a service in the **Service** filter at the top.

To upgrade, download `hud.ndjson` from the newer release tag, fill in the IDs again, and import it the same way.

## Install the alert rules

`rules/hud-alerts.yml` is a standard Prometheus rule file (top-level `groups:`), evaluated against the Hud SDK metrics in AMP.

### Via the AWS console

1. Open the [Amazon Managed Service for Prometheus console](https://console.aws.amazon.com/prometheus/) in the region of your workspace.
2. Select the workspace that receives the Hud SDK metrics.
3. Open the **Rules management** tab and click **Add namespace**.
4. Name the namespace (for example `hud`), upload `hud-alerts.yml`, and click **Create**.
5. To route the alerts (Slack, PagerDuty, SNS, ...), configure **Alert manager** in the same workspace.

### Via the AWS CLI

```bash
aws amp create-rule-groups-namespace \
  --workspace-id <workspace-id> \
  --name hud \
  --data fileb://hud-alerts.yml

# Upgrading an existing namespace:
aws amp put-rule-groups-namespace \
  --workspace-id <workspace-id> \
  --name hud \
  --data fileb://hud-alerts.yml
```

## What the alerts do

`HudFlowErrors` fires for each error (`issue_fingerprint`) on a flow (HTTP endpoint, queue consumer, custom flow, ...) that, in the window from 20 to 10 minutes ago, has:

- at least 1 error, and
- an error rate of at least 1% of the flow's invocations (or of its messages, for SQS flows).

HTTP 4xx responses are excluded. Each alert carries `service_name`, `deployment_environment_name`, `endpoint_method` and `endpoint_path` labels so it can be routed and grouped.
