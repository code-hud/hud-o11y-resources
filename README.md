# hud-o11y-resources

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
│   ├── dashboards/          # NDJSON saved-object exports
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
    └── scripts/             # unify.py, collector-configs.py, quick-create-link.sh
```

Releases are tagged the same way as the Hud SDK releases. Use the tag that matches the SDK version you run.

```bash
VERSION=<release tag>   # e.g. the SDK version you run
git clone --depth 1 --branch "${VERSION}" https://github.com/code-hud/hud-o11y-resources
cd hud-o11y-resources
```

Commands in both guides run from the repository root.

## License

Copyright Hud. Licensed under the [Apache License, Version 2.0](LICENSE).
