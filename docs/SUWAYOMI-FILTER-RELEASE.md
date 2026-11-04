# Suwayomi filter release acceptance

The filter/cache repair was deployed on 2026-11-04 at 17:08 UTC (22:38 IST) from
runtime source commit `300ccdbd0bd547d371a53da6baf11965f404b1a8`. The activation
command completed in 1.67 seconds; this is command duration, not measured downtime.

Only `suwayomi_graphql_filter.py` changed among 43 owned runtime assets. The
explicit `suwayomi-filter` profile selected only the filter process. The general
profile correctly refused this candidate. Java, Caddy, dashboard, supervisor, SSH
and every unrelated service retained their process start identities. The boot
alias, intentional DNS pins and four disabled markers remained intact.

## Behavior

The cache now distinguishes an unknown snapshot, a successful empty snapshot and
a failed lookup. Empty snapshots use the existing 30-second TTL. Concurrent reads
share a refresh; waiters have a bounded wait, and failed lookups use a cooldown.
Transient errors preserve the previous successful snapshot. A recognized feed
without any successful snapshot receives 503 rather than unrestricted updates.

Recognized update feeds keep the Reading, unread and in-library constraints.
The installed backend ignores `mangaId: {in: []}`. Read-only probes established
that `mangaId: {equalTo: 0, notEqualTo: 0}` returns no matches; that contradiction
is used for a known-empty Reading category. This does not assume ID zero is absent.
Nonempty snapshots retain the existing `in` representation. Other queries and
mutations pass through unchanged.

Malformed recognized requests return 400 before category lookup. Malformed
category responses are not cached as successful emptiness. GET and POST preserve
upstream HTTP errors and relevant headers. Response lengths and generated
Server/Date headers are emitted once. Importing the module performs no runtime
file, log, directory, process, thread or network work.

## Acceptance

- All 314 integrated Python tests passed in 21.997 seconds, with two optional
  network-dependent Caddy checks skipped. The 38 focused filter tests passed.
- Native copied recovery ran with real temporary filter processes after bundle
  deletion. It stopped the candidate, restored prior bytes/permissions, restarted
  the prior filter and passed repeated recovery. This isolated proof used
  process-presence health; no failing phone deployment was induced.
- After activation, all 43 live hashes, owners and target permissions matched.
  Journals were `COMPLETED` and `COMMITTED`.
- Benign GraphQL requests returned 200 through filter :4566, native bridge :4567
  and backend :4569. A recognized feed returned only Reading/unread results even
  when its input requested opposing filter values. A malformed recognized request
  returned 400. Response headers were unique.
- The supervisor reported 11 healthy services and four intentionally disabled
  services. No update mutation or download was triggered by acceptance probes.
  User IDs and response content remained on the phone; only sanitized results
  were exported. No category membership was changed for testing.

## Recovery

Keep the phone transaction directory while backout is needed:

```text
$HOME/.cache/mobile-server-suwayomi-filter-300ccdbd-20261104T170655Z/transaction
```

For interrupted activation, use the copied command:

```sh
tx="$HOME/.cache/mobile-server-suwayomi-filter-300ccdbd-20261104T170655Z/transaction"
python "$tx/recovery_lifecycle/release_activation.py" recover \
  --transaction-dir "$tx" --namespace-root /data/data/com.termux/files
```

To undo a completed release intentionally, replace `recover` with `rollback`.
The profile comes from the validated journal, not a recovery CLI override.
Neither operation rolls back library data, scheduler history or credentials.

The scheduler remains stopped at this point. Its supervised lifecycle repair is
the next approved slice. JVM/DNS replacement, catch-up scheduling and generalized
GET feed rewriting remain outside this release.
