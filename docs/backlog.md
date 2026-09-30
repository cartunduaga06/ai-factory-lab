# Trello backlog bridge

Set `FACTORY_TRELLO_BACKLOG_LIST_ID` to the Sprint list ID and
`FACTORY_TRELLO_READY_LABEL_ID` to the READY label ID. Both are required to
enable the bridge. The existing `FACTORY_TRELLO_KEY` and
`FACTORY_TRELLO_TOKEN` authenticate reads; `GITHUB_WRITE_TOKEN` must be able to
create Issues in `FACTORY_GITHUB_REPO`. Leave the two backlog IDs unset to keep
the existing GitHub-only intake behavior.

An open card is eligible when it is in the configured Sprint list, carries the
configured READY label, and every item in every checklist named `Dependencies`
is checked. A card with no `Dependencies` checklist has no declared blockers.
Malformed card or checklist data, and failed reads, stop reconciliation before
creating an Issue. The card is fetched again immediately before creation.

`python -m factory sync-backlog` reconciles the whole Sprint list. A verified
Trello webhook consumer may call `python -m factory sync-backlog --card-id ID`
for the card named in an event. The consumer must verify webhook authenticity
before invoking the command. `run` and each `watch` iteration also reconcile
the list before existing GitHub Issue intake; this periodic pass is the safety
net for missed events. The bridge creates no agent run and never dispatches.

Every created Issue carries `factory-ready` and a stable WorkItem marker in its
body. SQLite stores a unique reservation and the final card-to-Issue link in
`backlog_links`. A retry first checks the link, then scans open and closed
Issues for the exact marker. If a write began but its outcome is unknown, a
retry will search but will not POST again when no marked Issue is visible.
This preserves at-most-once creation under ambiguous network failures; an
operator must investigate an unresolved `POSTING` row. Do not manually reset it
without confirming GitHub has no matching Issue. There is no way to guarantee
both automatic progress and exactly-once creation after an ambiguous HTTP
response without a provider idempotency key.

The existing GitHub intake persists the new Issue as a `FactoryTask`; its E1
`IssueMaterialized` and `WorkItemReady` events then appear in the task trace.
`SqliteAuditEventStore.for_work_item("trello", card_id)` follows the durable link
to that trace. Status feedback to the source card is reserved for E6.

## Authorized sprint execution

When the Trello backlog IDs are configured, `run` and `watch` require an
explicitly authorized sprint. They materialize only its current WorkItem through
the existing E2 service, then permit the GitHub runtime to select only that
item's linked Issue. Other eligible Issues may be ingested, but cannot be
dispatched while this sprint path is enabled. With no authorization, the worker
is idle. The existing GitHub-only runtime remains available when Trello backlog
configuration is absent for the one-time E3 bootstrap.

Create a JSON manifest with ordered card IDs. Dependencies name earlier entries
as `trello:<card-id>`:

```json
{
  "sprint_id": "sprint-2026-10-a",
  "items": [
    {"external_id": "cardA"},
    {"external_id": "cardB", "dependencies": ["trello:cardA"]}
  ]
}
```

`python -m factory sprint plan --manifest sprint.json` reads current Trello
snapshots and existing local state, prints order, eligibility and blockers, and
does not persist or materialize anything. It requires an existing initialized
database so planning cannot create one. `sprint authorize --manifest sprint.json`
performs the same validation and persists the immutable snapshots and WIP=1
policy. Repeating the same authorization is idempotent; changing the manifest
under the same sprint ID is refused. The provider card must continue matching
its authorized snapshot at each E2 read, including the read just before issue
creation. A changed card pauses the sprint for review.

`sprint status` reports the durable position and state. A WorkItem that reaches
`WAITING_HUMAN`, `BLOCKED`, `FAILED` or `CANCELLED` pauses the sprint.
After human review, `factory run` can reconcile the PR while the sprint is
paused; it cannot dispatch another task. Then use
`sprint resume --sprint-id ID`. `sprint pause`, `sprint request-human`, and
`sprint cancel` are explicit local decisions; they never merge or deploy.
The next worker pass advances past a completed item and selects the next one.
Authorization, selection, pause, resume, advance and completion are stored as
append-only facts in E1 `audit_events`; `SqliteAuditEventStore.for_sprint(ID)`
reads them. No scripts or arbitrary actions are accepted in a manifest.
