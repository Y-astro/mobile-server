# Bounded parallel download execution

Design: [bounded parallel downloads](../specs/2026-11-05-bounded-parallel-downloads-design.md).
User authorized chapter/cross-source parallelism and conditional page parallelism.
Codex owns design, acceptance, integration and activation; external Gemini 3.8 Flash
High workers perform bounded build and implementation tasks. The referenced
writing-plans helper remains unavailable; this file records the plan directly.

1. **Unmodified build foundation.** Check out the exact official commit in an
   ignored directory. Prove compatible tools and `:server:shadowJar`, preserving
   accurate version metadata. Record artifact/toolchain/source provenance and
   relevant existing tests. No product patches or phone changes in this task.
   Stop with a concrete blocker if two build/tool attempts add no useful evidence.
2. **Chapter concurrency patch.** After baseline acceptance, dispatch one fresh
   implementation worker in an isolated upstream worktree. Add bounded claims,
   same-source lanes and shared admission at the responsible downloader seams.
   Preserve the design's six-source fairness and all queue/output/error contracts.
   Track a small patch set and tests in the maintained repository. Review actual
   overlap, cancellation/reorder/output and cap proofs before integration.
3. **Resource and page gate.** Compare unmodified and chapter-parallel builds with
   the same fake-source workload. Add bounded page workers only if correctness,
   fairness and resource evidence support it. Otherwise retain serial pages and
   finish the user's higher-priority chapter/cross-source outcome. A safe phone
   companion harness may be needed; never use private production data as a fixture.
4. **Artifact release support.** After the separate lifecycle repair is accepted,
   implement explicit single-JAR ownership and activation/backout policy. Inspect
   observed JAR metadata before choosing its canonical mode. Preserve existing
   profiles and the immutable historical ownership baseline. Prove native copied
   recovery with a retained prior artifact, stopped candidate and repeated backout.
5. **Activate and accept.** Codex verifies source/patch/artifact provenance, current
   live source/JAR fingerprints, space, data/schema compatibility and disabled
   intent. Activate with backend-only shutdown and retained rollback. Verify native
   and portal health, queue/settings compatibility and unrelated service identities.
   Monitor the bounded accepted workload; revert software if acceptance fails.

Do not start the JAR rollout while the current lifecycle transition is unresolved.
Keep current server/filter releases intact during preparation. Do not weaken tests,
invent a supported setting, lower six-source concurrency or increase JVM heap to
declare success. Record every accepted commit, artifact and remaining limitation
in the durable progress ledger; stop when the design's outcome is proven.
