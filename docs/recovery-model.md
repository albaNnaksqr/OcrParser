# Recovery Model

OcrParser separates recovery into local parser recovery and distributed platform
recovery. Local recovery avoids repeating expensive work inside one machine.
Platform recovery keeps long-running shared-storage jobs observable and
claimable when workers or network paths fail.

## Local Parser Recovery

The parser writes output artifacts and status sidecars for completed files.
Before reprocessing, it can check whether the expected artifacts already exist
and are complete enough to trust.

This protects common cases:

- the process is interrupted after some PDFs finish;
- a directory job is restarted with the same output directory;
- a manifest shard is retried and should not overwrite valid completed files;
- a partial output should not be treated as success.

For clean benchmark runs, disable resume and force reprocessing. For production
runs, keep resume behavior enabled unless you explicitly want to regenerate all
outputs.

## Manifest Snapshot

Distributed jobs use a manifest to make a folder scan explicit:

1. The control plane records the requested input/output paths.
2. A scan unit lists visible PDFs and writes a manifest.
3. The manifest is split into shards.
4. Workers claim shards and invoke the parser with shard-specific input.

This design avoids relying on each worker to independently rediscover a moving
directory tree. The manifest is the execution snapshot.

## Manifest Freeze And Integrity

**Manifest freeze** records the snapshot that a job is supposed to execute:
file count, byte count, shard count, and scan/shard state. A frozen manifest is
the job's contract.

**Manifest integrity** checks whether the control-plane metadata and manifest
files still agree. It is useful for catching problems such as:

- a manifest file missing from shared storage;
- shard files missing;
- shard file counts not matching the manifest count;
- metadata count mismatches after a failed scan or manual file operation.

In a multi-host deployment, the control server can only verify files it can
read. If workers can see a shared path that the control server cannot mount,
integrity may report missing files even though a worker could still read them.
For the most useful integrity view, mount the same shared storage on the control
host and workers.

## Shard Leases And Reclaim

When a worker claims a shard, the control plane records assignment and lease
metadata. Heartbeats renew active work. If a worker disappears and the lease
expires, the shard can be reclaimed by another eligible worker.

This protects common platform failures:

- worker process exit;
- host reboot;
- network interruption;
- parser subprocess termination before a terminal shard update is delivered.

The platform tracks attempts so stale work is visible instead of silently
overwriting the current shard state.

## Worker Update Spool

Workers may need to report shard progress or terminal state while the control
API is temporarily unavailable. Update records can be written to a local spool
and replayed later.

Malformed or permanently rejected records are quarantined so one bad update does
not block later valid updates for the same worker.

## Stop Semantics

Stopping a job is cooperative:

- unclaimed shards are marked stopped;
- running shards are asked to settle;
- expired running leases can be finalized through the same stale path;
- terminal job summaries are calculated after shard state is stable.

This avoids pretending that a distributed job stopped instantly when a worker is
still finishing or reporting the current shard.

## Operational Checklist

- Put input, output, and manifest paths on storage visible to all participating
  workers.
- Mount the same shared storage on the control host if you want full manifest
  integrity checks from the UI.
- Keep worker `server_id` values unique.
- Use small shard sizes for failure isolation and larger shard sizes for lower
  scheduling overhead.
- Tune lease timeout to be longer than normal shard heartbeat gaps but short
  enough to recover abandoned work.
