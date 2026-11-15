# hud-opensearch-resources

Observability resources for data produced by the Hud SDK. The repository has two parts:

| Directory | Contents |
| --- | --- |
| [`opensearch-ui/`](opensearch-ui/README.md) | OpenSearch dashboards, the loader that adapts them to your workspace, Prometheus alert rules for the same Hud metrics, and the loader's tests. |
| [`deploy/`](deploy/README.md) | CloudFormation templates and Helm values that deploy the [Hud OpenTelemetry collector](https://github.com/code-hud/hud-otel-collector) and the Observability Stack to AWS, plus the scripts that generate and check them. |

```
.
├── README.md
├── opensearch-ui/
│   ├── README.md
│   ├── dashboards/
│   │   ├── hud-all-dashboards.ndjson      # all dashboards in one export: the file to import
│   │   └── individual-dashboards/         # one export per dashboard, for reference
│   ├── rules/               # Prometheus alert rules
│   ├── scripts/
│   │   └── dashboard-loader.py
│   └── tests/
│       └── test_dashboard_loader.py
└── deploy/
    ├── README.md
    ├── alternatives.md
    ├── aws/                 # CloudFormation templates and their sources
    ├── helm/                # collector Helm values
    └── scripts/             # unify.py, collector-configs.py, quick-create-link.sh, publish-templates.sh
```

## Releases

This repository has its own semver versions, independent of the Hud SDK. A release is cut by pushing a tag: the
`publish-templates` workflow uploads the CloudFormation templates to `templates/<version>/` in the
`hud-cloudformation-templates` bucket, and copies them to the flat `templates/` prefix that the quick-create links use.
A version that is already published isn't overwritten.

Clone the latest release, not a tag that matches your SDK version:

```bash
VERSION=$(git ls-remote --tags --refs --sort=-v:refname https://github.com/code-hud/hud-opensearch-resources | head -1 | cut -d/ -f3)
git clone --depth 1 --branch "${VERSION}" https://github.com/code-hud/hud-opensearch-resources
cd hud-opensearch-resources
```

Commands in both guides run from the repository root.

Check that the release supports the Hud SDK version you run:

| Resources version | Supported Hud SDK versions |
| --- | --- |
| 1.12.4 | 1.12.1 and later |

The minimum SDK version is known when a release is cut. The upper bound is added later, when an SDK release changes
telemetry names; that SDK release ships together with a new release here.

## License

Copyright Hud. Licensed under the [Apache License, Version 2.0](LICENSE).
