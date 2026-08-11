# OcrParser production deployment bundle

A public Ansible + systemd reference for deploying OcrParser Control and
worker agents onto plain Linux hosts. It installs one exact, verified
release as a non-editable virtualenv build and runs it under systemd. It
does **not** provision infrastructure: PostgreSQL, the shared filesystem,
and the model service are all external systems that you own and operate;
this bundle only ever *validates* that they are reachable and correctly
configured, never provisions, mounts, formats, or starts them.

## Read this first: what sudo will do

Ansible performs only the controlled `become` operations below; it does not
execute undeclared arbitrary shell commands. Paths in this table are defaults.
If inventory overrides them, `playbooks/privilege-check.yml` prints the exact
effective paths for each host before any mutation.

| Equivalent system operation | Default target | Why sudo is required |
| --- | --- | --- |
| `sudo -n true` | No filesystem write | Read-only proof that the deployment account can escalate non-interactively; failure stops before a prompt or host mutation. |
| Create the system user and group | User `ocr-platform`, group `ocr-runtime` | Run Control and Workers under a dedicated non-root identity. |
| Create directories and set owner/mode | `/opt/ocr-platform` and its `source/`, `venv/`, and `wheels/` children | Store the commit-, tag-, SHA256-, and provenance-verified release and prevent ordinary users from replacing production code. |
| Write root-owned mode-`0600` environment files | `/etc/ocr-platform/control.env`, `/etc/ocr-agent/worker.env` | Let systemd load runtime configuration and secrets while preventing ordinary accounts from reading them. |
| Create Worker state directories and set permissions | `/var/lib/ocr-agent/<worker-id>/work`, `/var/lib/ocr-agent/<worker-id>/spool`, `/var/log/ocr-agent/<worker-id>` | Persist work state, disconnected replay data, and logs across restarts and network interruptions. |
| Write systemd units | `/etc/systemd/system/ocr-platform-control.service`, `/etc/systemd/system/ocr-agent-worker.service` | Let systemd manage startup, restart, isolation, and boot-time enablement. |
| `systemctl daemon-reload`, `enable`, and `start/restart` | The Control and Worker units above | Load changed units, enable boot startup, and restart only the service whose release or configuration changed. |
| Create and clean the optional canary subtree | `<ocr_shared_root>/canary` | Only when canary is explicitly enabled, hold public synthetic inputs and artifacts and remove task files afterward. |

Sudo is **not** used to install operating-system packages, create PostgreSQL
or database users, mount or format storage, start model services, change
firewall/DNS/TLS/SSH configuration, or downgrade the database. Control
migrations use your separately supplied database account and are not sudo
operations.

This directory is public. Every example file (`inventory.example.yml`,
`group_vars/all.example.yml`) contains only non-resolvable `.invalid`
placeholders. Your real inventory, group vars, and secrets belong in a
separate, private repository that is never committed here.

## How sudo is authorized

This is a systemd production install, not an unprivileged quickstart. Before
any mutation, `playbooks/privilege-check.yml` prints the exact host-specific
contract. Services run as `ocr-platform`, not root.

The default `become_ask_pass=false` never opens a surprise password prompt.
The read-only probe accepts root, `sudo -n true`, or credentials intentionally
supplied with `--ask-become-pass`; otherwise it stops before creating anything.

## Topology

- Exactly **one Control host** (`ocr_control` group) serving the HTTP API
  and coordinating work.
- **N worker hosts** (`ocr_workers` group), each with a unique
  `ocr_worker_server_id`, polling Control and processing shards.
- **External PostgreSQL** — Control's database. Provisioned and operated by
  you; the bundle asserts it is reachable and, if
  `ocr_control_require_current_migrations` is set, that migrations are
  current. It never runs `CREATE DATABASE`, never provisions a user, and
  never downgrades the schema.
- **External shared filesystem** — mounted at `ocr_shared_root` on every
  host by you (NFS, a cloud filesystem, whatever fits your environment).
  The bundle only asserts the mount and the `ocr_input_root` /
  `ocr_output_root` / `ocr_platform_root` subpaths exist and are usable.
- **External model service** (e.g. a DotsOCR endpoint) — used only by
  canary and only referenced by URL, engine, and model name. The bundle
  never deploys, starts, or holds credentials for it beyond the name of an
  environment variable.
- TLS termination in front of Control is your responsibility. Control binds
  plain HTTP on `ocr_control_listen_host:ocr_control_listen_port`;
  `ocr_control_url` is what workers and operators use externally.

## Operator prerequisites

Before running anything against real hosts:

1. PostgreSQL is running, reachable from the Control host, and its
   connection URL is available as the environment variable named by
   `ocr_database_url_secret_var` on the Ansible controller (or wherever you
   run `ansible-playbook`) — never as a literal value in any inventory file.
2. The shared filesystem is already mounted at `ocr_shared_root` on Control
   and on every worker, with `ocr_input_root`, `ocr_output_root`, and
   `ocr_platform_root` present and writable by `ocr_service_user`.
3. SSH access and `become` privileges to every host in `ocr_control` and
   `ocr_workers`.
4. A private repository, separate from this public one, holding your real
   `inventory.yml` and any optional `group_vars/all.yml`, never committed
   here.
5. The Ansible controller uses the pinned 2.19 series from `requirements.txt`;
   every managed host has Python >= `ocr_min_python_version` available at
   `ocr_python_interpreter`.
6. `scripts/validate_inventory.py` run and passing against your private
   inventory before every rollout — Ansible's own assertions duplicate the
   most safety-critical of these checks, but the validator catches
   structural and secret-handling mistakes earlier and with a clearer
   message.
7. Native parser runtime libraries installed on every managed host. On
   Debian/Ubuntu use `libgl1`, `libglib2.0-0`, and `libzbar0`; on a
   RHEL-family host use the equivalent `mesa-libGL`, `glib2`, and `zbar`
   packages. `preflight.yml` verifies the OpenCV and pyzbar imports and fails
   with an actionable message when these libraries are absent.

## Compact private inventory and secrets separation

- Copy `inventory.example.yml` to `inventory.yml` in your **private**
  deployment repository. It requires only the release tag, shared root,
  PostgreSQL host, two secret environment-variable references, one Control
  URL, and the Worker host aliases. Copy `group_vars/all.example.yml` only
  when advanced overrides are needed.
- The service identity, install paths, shared subtrees, Control bind address,
  PostgreSQL port, rollout policy, Worker IDs, and Worker runtime paths are
  derived from safe defaults. The previous expanded inventory remains valid.
- No inventory or group-vars file may hold a literal secret. Only keys
  ending in `_secret_var`, `_env_var`, or `_secret_ref` are permitted for
  anything that looks like a token, password, key, or database URL; each
  names an environment variable that the roles resolve at run time with
  `no_log: true`. The validator rejects anything else.
- Resolve those environment variables however your organization already
  manages secrets (a vault, a CI secret store, an operator's shell) — this
  bundle deliberately does not include a secrets backend of its own.

## Release identity resolution

For releases that publish `deployment-manifest.json`, `ocr_release_tag` is the
only required release field. The controller downloads the manifest from that
GitHub Release, or from `ocr_release_manifest_url` for an internal mirror.
Managed hosts still verify the tag target, clean source checkout, wheel sha256,
and installed wheel provenance.

Tag CI preserves the wheel and `deployment-manifest.json` together as the
release-operator artifact. The operator must attach both unchanged files to
the matching GitHub Release; the bundle never manufactures identity locally
on a managed host.

Historical `v0.4.0` and offline deployments provide `ocr_release_commit`,
`ocr_source_repo_url`, `ocr_wheel_url`, and `ocr_wheel_sha256` together.
Partial or mixed identities are refused.

## Command order

Run every playbook from `deploy/production/ansible/`, pointed at your
private inventory:

```
ansible-playbook -i /path/to/private/inventory.yml playbooks/privilege-check.yml
python3 scripts/validate_inventory.py /path/to/private/inventory.yml --json
ansible-playbook -i /path/to/private/inventory.yml playbooks/preflight.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/control.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/workers.yml
ansible-playbook -i /path/to/private/inventory.yml playbooks/verify.yml
# optional, opt-in:
ansible-playbook -i /path/to/private/inventory.yml playbooks/canary.yml -e ocr_canary_enabled=true
```

- **privilege-check.yml** — read-only: explains every privileged path and
  action, verifies non-interactive sudo (or an explicitly supplied become
  credential), and changes nothing.
- **preflight.yml** — installs the exact, checksum-verified release into a
  virtualenv on every host in `ocr_control` and `ocr_workers`. Fails closed
  on any release-identity mismatch, insufficient disk, too-old Python, or a
  dirty/wrong source checkout. Starts nothing.
- **control.yml** — configures and (re)starts the Control systemd service.
  `serial: 1`, `max_fail_percentage: 0` — there is exactly one Control host,
  and this play refuses to proceed on any other topology or on failure.
- **workers.yml** — rolling deploy across `ocr_workers`, batch size
  `ocr_rollout_serial`, aborting the whole play if failures in a batch
  exceed `ocr_rollout_max_fail_percentage`. Run only after `control.yml`
  succeeds: workers register against `ocr_control_url` and refuse to start
  otherwise.
- **verify.yml** — checks Control's `/healthz`, `/readyz`, and
  `/source.json`, confirms every inventoried worker has registered with the
  expected release, and asserts the external PostgreSQL and shared-filesystem
  dependencies are reachable. Writes a redacted report (see below). Run
  after every `control.yml` / `workers.yml` / `rollback.yml`.
- **canary.yml** — opt-in only; refuses to run unless
  `ocr_canary_enabled=true` is passed explicitly. Run after `verify.yml`
  passes.

## Check-mode limitations

`ansible-playbook --check` is **not** a reliable dry run for this bundle.
In particular:

- The release-identity, Python-version, and disk-space assertions in the
  `common` role run real commands (`git`, `df`, a Python subprocess) and
  report accurately in check mode, but the `git checkout`, wheel download,
  and virtualenv/pip install tasks are skipped by Ansible's check-mode
  semantics — so the build-provenance assertions that follow them cannot
  run and will be skipped too, not verified.
- systemd service restarts in the `control` and `worker` roles are no-ops
  under `--check`, so `verify.yml` run in the same check-mode pass will
  observe the *previous* running release, not the one you are checking.
- Use `--check` only to sanity-check host connectivity and variable
  resolution before a real run, never as evidence that a rollout will
  succeed.

## Failure and rollback procedure

If `control.yml`, `workers.yml`, or `verify.yml` fails partway:

1. Do not re-run the failed playbook against the whole fleet blindly if the
   failure was in `workers.yml` under a serial batch — some workers are
   already on the new release and some are not. Re-running is safe (every
   task is idempotent) but confirm with `verify.yml` first which hosts are
   in which state.
2. To go back to a known-good release, use `rollback.yml`. It requires,
   supplied explicitly with `-e` at invocation (never defaulted):
   - `ocr_rollback_release_tag`, `ocr_rollback_release_commit`,
     `ocr_rollback_wheel_url`, `ocr_rollback_wheel_sha256` — the exact prior
     release identity.
   - Exactly one of `ocr_rollback_migration_compatible=true` (the prior
     release's code is compatible with the *current*, not-downgraded,
     database schema) or `ocr_rollback_restore_plan_reference="..."` (a
     pointer to your own out-of-band plan for reconciling schema and code).
   This bundle never downgrades the database itself; it only refuses to
   proceed without one of the two compatibility assertions above.
3. `rollback.yml` rolls Control back first (`serial: 1`,
   `max_fail_percentage: 0`), then workers with the same
   `ocr_rollout_serial` / `ocr_rollout_max_fail_percentage` guard as a
   forward rollout.
4. Run `verify.yml` again after any rollback.

Example:

```
ansible-playbook -i /path/to/private/inventory.yml playbooks/rollback.yml \
  -e ocr_rollback_release_tag=v0.3.2 \
  -e ocr_rollback_release_commit=<40-hex commit> \
  -e ocr_rollback_wheel_url=https://artifacts.example.invalid/ocrparser/v0.3.2/ocrparser_platform-0.3.2-py3-none-any.whl \
  -e ocr_rollback_wheel_sha256=<64-hex sha256> \
  -e ocr_rollback_migration_compatible=true
```

## Reports and redaction

`verify.yml` and `canary.yml` write one structured JSON fact file per run
under `ocr_report_dir` (default `reports`, controller-local, gitignored).
Reports contain only an allow-listed release identity, health result, worker
counts, and (for canary) completion status. They do not contain host names,
addresses, paths, document text, a secret, token, or database URL. The files
are deployment evidence rather than repository source; keep them outside
version control even though the renderer also applies defensive redaction.

## No Remote Admin

`ocr_control_enable_remote_admin` defaults to `0` and this bundle never
overrides it. Weakening it, along with any other setting flagged in
`group_vars/all.example.yml` as affecting the documented security posture
(`ocr_control_require_postgres`, `ocr_control_auto_migrate`,
`ocr_control_require_current_migrations`, `ocr_control_api_auth_required`,
`ocr_control_allow_saved_model_profile_keys`), is a deliberate, per-deployment
decision you make in your private `group_vars/all.yml` — never something
this bundle does for you, and never something to change without recording
why.
