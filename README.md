# hud-o11y-resources

Ready-made OpenSearch dashboards and Prometheus alert rules for data produced by the Hud SDK.

| Path | Contents |
| --- | --- |
| `dashboards/` | The Hud dashboards ("Hud: ..."), together with their panels, queries and index pattern, as NDJSON saved-object exports from OpenSearch UI or OpenSearch Dashboards (with related objects included). |
| `scripts/dashboard-loader.py` | Adapts the dashboards to your environment (workspace, data sources, index pattern, OpenSearch Dashboards version) and checks the result before you import it. |
| `rules/hud-flow-errors.rules.yaml` | Prometheus rule groups (Hud flow error alerts) for the Hud SDK metrics. |
| `rules/hud-flow-*.rules.yaml`, `rules/hud-function-*.rules.yaml` | Prometheus rule groups for latency degradation (`week-over-week`, `day-over-week`, `latency-spike`, and an optional function-level `day-over-week`) and the alpha `error-rate-spike`. One file per namespace, named after the entity it alerts on. |

Releases are tagged the same way as the Hud SDK releases. Use the tag that matches the SDK version you run.

## Download

```bash
VERSION=<release tag>   # e.g. the SDK version you run
git clone --depth 1 --branch "${VERSION}" https://github.com/code-hud/o11y-templates
cd o11y-templates
```

## Prerequisites

- Python 3.9 or later, to run `scripts/dashboard-loader.py`. It uses only the standard library.
- One of:
  - an **OpenSearch UI** application with a workspace for the Hud data, and permission to import saved objects into it;
  - a self-managed **OpenSearch Dashboards** 3.9 or later.
- The Hud SDK metrics in Prometheus or Amazon Managed Service for Prometheus (AMP), connected as a Prometheus data connection. Most panels are PromQL queries against them.
- Hud forensics exported as OTLP logs to OpenSearch, in indices matching `logs-otel-v1*` (or another pattern, see below). The investigations and exceptions panels are PPL queries against these logs.

## Install the dashboards

### 1. Connect your data sources

**OpenSearch UI:**

1. **OpenSearch domain.** In the AWS console, open **Amazon OpenSearch Service** > **OpenSearch UI (Dashboards)** > your application, and add the OpenSearch domain that holds the Hud logs. Its domain endpoint looks like `https://search-<domain-name>-<id>.<region>.es.amazonaws.com` (shown on the domain's page in the console). Then, in OpenSearch UI, open the workspace and associate the domain with it under **Data sources**.
2. **Prometheus.** In the same workspace, open **Data sources** > **Create data connection** > **Amazon Managed Service for Prometheus**, select the AMP workspace that receives the Hud SDK metrics, and give the connection a name (for example `hud_amp`).

**Self-managed OpenSearch Dashboards:** under **Data sources** > **Create data connection** > **Prometheus**, connect the Prometheus that receives the Hud SDK metrics and give the connection a name. The logs panels query the cluster OpenSearch Dashboards is connected to, unless you pass a data source (step 2).

### 2. Adapt the dashboards

`scripts/dashboard-loader.py` reads every `*.ndjson` file under `dashboards/`, fills in your environment, checks the result, and writes all the objects to a single file, `out/hud.ndjson`. The files under `dashboards/` are not modified.

**OpenSearch UI:**

```bash
python3 scripts/dashboard-loader.py --target opensearch-ui \
  --workspace-id <workspace id> \
  --opensearch-data-source-id <data source id> \
  --prometheus-connection <connection name> \
  --prometheus-arn arn:aws:opensearch:<region>:<account>:datasource/<connection name>
```

**Self-managed OpenSearch Dashboards:**

```bash
python3 scripts/dashboard-loader.py --target osd \
  --osd-url https://<your dashboards host> --osd-user <user> \
  --prometheus-connection <connection name>
```

`--osd-url` reads the OpenSearch Dashboards version from `/api/status`, with the password taken from the `OSD_PASSWORD` environment variable. If the script can't reach OpenSearch Dashboards, pass the version instead, for example `--osd-version 3.10.0`.

| Option | Value | Where to find it |
| --- | --- | --- |
| `--workspace-id` | Your workspace ID. Required for OpenSearch UI. For OpenSearch Dashboards, only if workspaces are enabled. | The part after `/w/` in the workspace URL, e.g. `abc123` in `https://application-…opensearch.amazonaws.com/w/abc123/app/…` |
| `--opensearch-data-source-id` | The ID of the OpenSearch data source that holds the Hud logs. Required for OpenSearch UI. For OpenSearch Dashboards, omit it to query the local cluster. | Open **Data sources** and click the domain; the ID is the last part of the page URL |
| `--opensearch-data-source-title` | The data source's name, shown in the query editor. Optional. | Same page |
| `--prometheus-connection` | The name of the Prometheus data connection from step 1. Omit it if you used the name the dashboards already have. | **Data sources** > **Data connections** |
| `--prometheus-arn` | The ARN of the Prometheus data connection. Required for OpenSearch UI. Not used for OpenSearch Dashboards. | `aws opensearch get-direct-query-data-source --data-source-name <connection name>`, or build it as `arn:aws:opensearch:<region>:<account>:datasource/<connection name>` |
| `--logs-index-pattern` | The index pattern of the Hud logs, if it is not `logs-otel-v1*`. | Your log pipeline's index settings |
| `--out` | The output directory (default `out/`). | |
| (arguments) | The files or directories to read, instead of `dashboards/`. | |

The script then checks that no placeholder is left, and that every reference, index pattern and dashboard link points to an object in the output. If any check fails, it lists the problems and writes nothing.

**About the links between the dashboards.** Clicking an endpoint or function opens its page with "All services" selected. Only OpenSearch Dashboards 3.10 and later can open a page with "All" preselected. For older versions, and for OpenSearch UI (which doesn't report its version), the script rewrites the links so the page opens on the service, and the flow, that the clicked item belongs to. All values are still available in the dropdowns. To override this, use `--links all-marker` or `--links version-neutral`.

### 3. Import

1. Open OpenSearch UI or OpenSearch Dashboards, and switch to the workspace if you use one.
2. Open **Assets** (without workspaces: **Dashboards Management** > **Saved objects**).
3. Click **Import** and select `out/hud.ndjson`.
4. Choose **Check for existing objects** > **Automatically overwrite conflicts**, and click **Import**. Keep the original object IDs (do not choose **Create new objects with random IDs**): the "click one for its page" links between the dashboards refer to them.
5. Go to **Dashboards** and open any of the dashboards whose names start with `Hud:`. Pick a service in the **Service** filter at the top.

On self-managed OpenSearch Dashboards you can also import with the API (add `/w/<workspace id>` before `/api` for a workspace):

```bash
curl -u <user> -X POST "https://<your dashboards host>/api/saved_objects/_import?overwrite=true" \
  -H 'osd-xsrf: true' --form file=@out/hud.ndjson
```

To upgrade, check out the newer release tag, run `scripts/dashboard-loader.py` again with the same options, and import the result the same way.

## Install the alert rules

`rules/hud-flow-errors.rules.yaml` is a standard Prometheus rule file (top-level `groups:`), evaluated against the Hud SDK metrics in AMP.

### Via the AWS console

1. Open the [Amazon Managed Service for Prometheus console](https://console.aws.amazon.com/prometheus/) in the region of your workspace.
2. Select the workspace that receives the Hud SDK metrics.
3. Open the **Rules management** tab and click **Add namespace**.
4. Name the namespace (for example `hud`), upload `hud-flow-errors.rules.yaml`, and click **Create**.
5. To route the alerts (Slack, PagerDuty, SNS, ...), configure **Alert manager** in the same workspace.

### Via the AWS CLI

```bash
aws amp create-rule-groups-namespace \
  --workspace-id <workspace-id> \
  --name hud \
  --data fileb://hud-flow-errors.rules.yaml

# Upgrading an existing namespace:
aws amp put-rule-groups-namespace \
  --workspace-id <workspace-id> \
  --name hud \
  --data fileb://hud-flow-errors.rules.yaml
```

## What the alerts do

`HudFlowErrors` fires for each error (`issue_fingerprint`) on a flow (HTTP endpoint, queue consumer, custom flow, ...) that, in the window from 20 to 10 minutes ago, has:

- at least 1 error, and
- an error rate of at least 1% of the flow's invocations (or of its messages, for SQS flows).

HTTP 4xx responses are excluded. Each alert carries `service_name`, `deployment_environment_name`, `endpoint_method` and `endpoint_path` labels so it can be routed and grouped.

### Degradation alerts

The files below alert on p90 latency degradation (and, for `hud-flow-error-rate-spike`, error-rate spikes) of Hud flows, using the `code_flow_duration_seconds` native histogram. Each file is its own AMP namespace (uploading a namespace replaces everything in it), so upload each under a different name:

| File | Alert | Interval | History needed |
| --- | --- | --- | --- |
| `hud-flow-week-over-week.rules.yaml` | `HudWeekOverWeekDegradation` | 1h | about 14 days |
| `hud-flow-day-over-week.rules.yaml` | `HudDayOverWeekDegradation` | 1h | about 8 days |
| `hud-flow-latency-spike.rules.yaml` | `HudLatencySpike` | 10m | about 1 day |
| `hud-function-day-over-week.rules.yaml` | `HudFunctionDayOverWeekDegradation` | 1h | about 8 days |
| `hud-flow-error-rate-spike.rules.yaml` | `HudErrorRateSpike` (critical, alpha) | 1h | about 8 days |

```bash
aws amp create-rule-groups-namespace --workspace-id <workspace-id> --name hud-wow --data fileb://hud-flow-week-over-week.rules.yaml
# update: aws amp put-rule-groups-namespace ... (same arguments)
```

Differences from Hud's own detection:

- No per-entity overrides, exclusion lists or session tags, and no issue lifecycle: AMP alerts are stateless firing/resolved, so Hud's cooldowns and dedup do not apply.
- The p90 is a native-histogram quantile, not Hud's DDSketch; the in-progress hour is not part of the current window; Hud's requirement of a degraded source function is not reproduced, so these alert more often than Hud.
- There is no flow-type selector, so custom and queue flows are included although Hud only runs degradation on endpoint and Kafka entities.
- `hud-function-day-over-week.rules.yaml` is inert until the `code_function_name` regex (`__NO_FUNCTION_SELECTED__`) is set to the functions you watch. It is expensive: it queries per-function series over 8 days.

## Development

`python3 -m unittest discover -s tests` runs the script's tests against the dashboards in `dashboards/`.

## License

Copyright Hud. Licensed under the [Apache License, Version 2.0](LICENSE).
