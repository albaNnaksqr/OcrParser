# v0.4.2 Release

English | [中文](release-v0.4.2.zh-CN.md)

`0.4.2` is a Control UI usability release for operators running large OCR
workloads. It changes the browser workspace only; Parser, Agent, Control HTTP,
database, migration, scheduling, manifest, and output contracts are unchanged.

## Workbench

- `#jobs` is the default queue and job investigation view.
- `#jobs/new` is a full-width, four-step job workflow. Draft values stay in
  memory, the existing backend preflight remains authoritative, and request-only
  API keys are never rendered in Review or persisted by the browser.
- `#workers` groups readiness, shared-path eligibility, capacity, resource,
  version, and spool evidence. Registration, scaling, and Remote Admin remain
  available under Advanced sections.
- `#system` owns Deployment Doctor, database and migration evidence, Model
  Profiles, capacity, audit, alerts, source, and license entry points.

## Deployment Doctor

Diagnostics remain read-only. The UI explains each supported finding, its
operational impact, and a recommended action. Migration drift displays the
explicit `status → plan → apply → verify` workflow; the UI does not execute a
migration, modify configuration, start a service, or invoke sudo.

## Validation

The release keeps the Python 3.10-3.12, installation-profile, PostgreSQL,
mock-E2E, deployment-bundle, and documentation gates. A separate Python 3.12
Playwright Chromium job installs the built wheel and checks UI package data,
authentication, navigation, Doctor rendering, Worker inspection, job preflight
and creation, desktop layouts, focus behavior, labels, and live regions.
