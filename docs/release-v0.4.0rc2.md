# v0.4.0rc2 Release Candidate

English | [中文](release-v0.4.0rc2.zh-CN.md)

`0.4.0rc2` supersedes `0.4.0rc1` and keeps the same v0.4 Control,
certification, observability, scheduling, and compatibility scope. Its only
runtime change fixes a worker event/log spool durability race found during the
RC review.

## Fix

During replay, the Agent previously read the pending JSONL file, awaited the
Control request, and then replaced the file from that old snapshot. A new
event or log appended during the request could therefore be removed without
ever reaching Control.

Replay is now serialized independently for events and logs. Successful or
permanently rejected records are acknowledged against the latest on-disk file,
so unrelated records appended during the request remain durable. File locks
are not held across network requests.

## Compatibility

This candidate does not change CLI, HTTP/OpenAPI, database migrations, event or
log payloads, spool JSONL format, status values, Agent lanes, Parser behavior,
manifest/output formats, or model integrations. The schema ceiling remains
migration `0020`.

## Candidate Integrity

The release wheel must be built from the clean commit referenced by the
`v0.4.0rc2` tag. The installed distribution and `/source.json` must report
version `0.4.0rc2`, that exact source revision, `dirty=false`, and
`release_build=true`.

The candidate requires the full automated matrix plus a short isolated
Control-outage/replay validation. It does not require a GPU service or another
four-hour soak. Final `v0.4.0` promotion remains subject to the RC observation
window with no additional tracked runtime changes.
