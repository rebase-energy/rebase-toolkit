# Working on `dev` in the Rebase ecosystem

This section is the same in every product repository (`platform`, `rebase-grid`,
`rebase-toolkit-backend`, `rebase-toolkit`). The canonical copy lives in
`distributed-agents/docs/ecosystem/CONTRIBUTING_DEV.md`.

## Push to `dev`

Run your product's focused tests locally, merge `dev`, push. Nothing else. The
integrator (gen-1) takes it from there:

1. within ~15 s the push is picked up and delivered on canonical: compile,
   secret scan and the manifest's deployability checks (no product suites);
2. the touched components are deployed to the shared dev environment and smoked;
3. the ecosystem integration tests chosen for your change (the contract tests of
   every contract you touched, plus what the selector adds within a 60 s budget)
   run against the live dev stack;
4. green: your commit is the accepted release and the environment advances a
   generation. Red: the candidate is rolled back and an agent opens a repair
   task scoped to your repository; the fix deploys through the same lane.

Expect ~4 minutes push → live for a code-only change; ~10 minutes when a
repair was needed. Every stage is stamped on your commit's row.

## Where to read the status

- The commit status `rebase/dev` on your sha (pending → success `live in dev,
  generation N` / failure `repair task …` / `blocked: …`), linking to the change.
- `python3 rebasectl/rebasectl.py status` and `explain-change <sha>` (from
  `rebase-ecosystem`), or the ecosystem app on gen-1:
  `/app-proxy/rebase-ecosystem/changes/<sha>`.
- A red push: the row says `integration_failed` with the failing test ids and
  the repair task id; the dev stack is already back on the last accepted release.

## Grouping a change across repositories

Add `.rebase/change.json` to each commit of the group:

    {"change_id": "CHG-123", "declared_scope": "additive-column",
     "products": {"platform": "dev", "grid": "dev"}, "spec_refs": []}

The watcher waits until every member is on its `dev`, deploys providers first
and advances one generation for the whole change. `declared_scope` starting
with `additive` lets a provider deploy before its consumers adapt; anything
else parks the provider until the adapt-consumer tasks (one per consumer
outside the change) are settled. `rebasectl submit --change-id … --scope …
--with repo:dev` writes the descriptor for you.

## Never

- change `specs/**` in `rebase-ecosystem` from an agent task or without an
  approved final turn (`spec_write_forbidden`, `spec_unapproved`);
- weaken or remove floor tests, contract tests or the quarantine list (the
  suite and its catalog are ecosystem-owned; edits only through human tasks);
- force-push `dev` or rebase it backward (`rebasectl sync-ecosystem` only
  fast-forwards);
- commit credentials: the secret scan blocks the push, and a push to `dev` is
  live within minutes.
