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
