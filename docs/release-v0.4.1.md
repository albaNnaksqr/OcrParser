# v0.4.1 Release

English | [中文](release-v0.4.1.zh-CN.md)

`0.4.1` is a deployment-focused maintenance release. It adds a production
Ansible + systemd reference bundle for one Control host and multiple Workers
without changing Parser, HTTP, database, manifest, output, scheduling, or
engine behavior.

## Highlights

- A compact inventory derives service identities, paths, Worker IDs, rollout
  settings, and secure Control defaults from a small infrastructure contract.
- A read-only privilege check explains every sudo-protected path and systemd
  action before any host mutation, and fails without prompting when
  non-interactive elevation is unavailable.
- Release identity resolution verifies the tag target, clean source revision,
  wheel SHA256, and installed build provenance.
- The exact release source carries a complete production dependency
  constraints file. The bundle refuses installation if that lock is absent,
  preventing dependency resolution from drifting between hosts or dates.
- Verify, optional synthetic canary, redacted evidence reports, rolling Worker
  rollout, and fail-closed rollback are included.

## Deployment boundary

The bundle validates but does not provision PostgreSQL, shared storage, model
services, TLS, DNS, firewall, or SSH configuration. Services run as a dedicated
non-root account after the controlled installation steps complete.

Follow the [production deployment guide](../deploy/production/ansible/README.md).
The `v0.4.1` GitHub Release publishes both the release wheel and
`deployment-manifest.json`; operators must use those exact assets.
