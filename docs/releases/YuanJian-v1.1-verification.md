# YuanJian v1.1 verification

Verified on 2026-09-14 on Windows. **This record covers unreleased work.** The
`__version__` string in `src/yuanjian_app/__init__.py` is still `1.0.0`; bumping it,
building a package and publishing a release are separate decisions that were **not**
made in this pass. The version number in this filename marks the intended next
release, not a state that has been shipped.

## Scope

Hardening round covering three workstreams, all on `main` above commit `0bdb2be`:

1. structural work on the HTTP boundary and the judgment module,
2. the C/D/E retention layers and the F-group anti-redundancy rules,
3. test coverage for `secret_store` and `http_api`.

The work was carried out by three agents (one architect owning `src/**`, one test
engineer owning `tests/**`, one lead integrating), under a written interface contract
that was deliberately kept out of the published tree. Every claim below was
re-derived by the lead from a separate run; the architect's and the test engineer's
own reports were treated as claims, not as evidence.

## Automated verification

- Full suite: **440 tests passed, three consecutive runs**, with `ResourceWarning`
  treated as an error (`unittest discover -s tests`). Up from 269 at `80b7541`
  (426 at the first commit of this round, plus 14 covering the layer-A condition
  corrected in the follow-up commit).
- Coverage, measured with the zero-dependency `tools/coverage_baseline.py`:
  **84.1 % overall** (6280 / 7468 executable lines), up from a **80.4 %** baseline
  (5589 / 6948).
  - `secret_store` **48.0 % → 100 %** (75/75). It previously had 37 lines of tests;
    the DPAPI failure paths, the non-Windows branch, the file-header check and the
    atomic-replace path were all uncovered.
  - `http_api` **78.5 % → 99.1 %** (753/760). The 71.1 % figure quoted in the task
    brief was stale and was corrected against measurement.
  - `retention` reaches 94.0 %.
- Privacy scan of the staged publication tree, **128 files**
  (`tools/privacy_scan.py --committed`): `safe=True blocked=0 findings=0`.
  The six files added by this round are included in that count.
- Scoring the coverage number honestly: **7 modules remain below 80 %** and all of
  them are outside this round's scope — `judgment_validation` 63.2 %,
  `external_sources` 64.5 %, `remote_ai` 67.4 %, `application` 68.5 %,
  `radar_scheduler` 71.1 %, `diagnostics` 73.1 %, `runtime` 79.5 %. They are listed
  rather than quietly omitted.

## Retention: layers C, D, E and the threshold trigger

The mechanism grew from two layers to five. C, D and E are new:

- **C — `judgment_jobs`** (default 7 days, configurable 1–180). Once a job has
  produced a judgment, its flow record is redundant. Union of two rules: succeeded
  jobs whose cluster already has a judgment and are older than the window, plus any
  status older than 180 days.
- **D — `external_runs`** (default 90 days, configurable 7–730).
- **E — `trend_snapshots` down-sampling.** 6 h window kept 7 days, 24 h kept 60 days,
  168 h kept 365 days, **720 h kept forever**. Long windows are already coarse
  aggregates, so discarding the short-window detail loses no trend. This implements
  a feature that `retention.py` had claimed in its docstring for several releases
  while the code did nothing.
- **Threshold trigger**: runs the full stack when the database exceeds 2 GB or any
  single table exceeds 500 MB, with a 6-hour minimum between runs. Manual triggers
  bypass the interval. The check is polled hourly, not every 5 minutes, because
  `dbstat` costs about 1.8 s on a 894 MB database.

## Retention: the measured effect, and what it does not mean

Run against a one-off copy of the real database (937.1 MB):

    rows deleted                51,736
    free pages                  0 -> 4,903 pages  =  +19.98 MB reusable
    db file bytes               937,140,224 -> 937,140,224  (unchanged)
    second run                  every layer 0, free-page delta 0
    first-run duration          8.2 s

**Free-page growth is the only visible evidence that the cleanup did anything.** With
`auto_vacuum=0` and no `VACUUM`, deleting rows never shrinks the file, so comparing
the file size before and after reports "nothing happened" and is actively misleading.
`run()` now returns `free_pages_before` / `free_pages_after` for exactly this reason,
and the suite asserts both that free pages grow and that the file size does not move.

**Do not read the 19.98 MB as space saved.** At a ~26 MB/day growth rate it is less
than a day of writes. The earlier claim in `CHANGELOG.md` that this work cut daily
growth "from ~26 MB to ~14 MB" was **not supported by measurement and has been
corrected**: the honest figure is **~26 MB to ~24.6 MB, about 5 %**.

The ceiling is unchanged and worth restating: `judgments` is 280.8 MB (about 31 % of
the database, about half of daily growth) and is protected by database triggers that
refuse `UPDATE` and `DELETE`. Total size **cannot** be stabilised without changing the
"judgments are immutable" product commitment, which is a product decision, not an
engineering one.

The single largest genuine win is C's **steady state**, not its one-off reclaim: the
jobs table accrues roughly 3,384 rows/day, so C removes about 0.95 MB/day, roughly
340 MB over a year.

## Anti-redundancy: F1 is a no-op, F2 and F3 are real

- **F1 was specified but not implemented, because measurement showed it is a no-op.**
  The design document asserted the `judgments` table lacked the
  `(cluster_id, provider, evidence_hash)` uniqueness that `judgment_jobs` has. It does
  not lack it: `database.py` declares `UNIQUE(cluster_id, provider, evidence_hash)`,
  and the write path uses `INSERT OR IGNORE`. Measured duplicate triples: **0**. The
  rule could prevent 0 rows, so it was replaced by a regression test that pins the
  constraint instead of by code that would have reported a 39 MB saving.
- **F2** compares the new content against the cluster's most recent judgment and skips
  an identical write. Measured reach: **2,200 rows ≈ 8.5 MB**.
- **F3** caps judgments per cluster (default 8, configurable 0–1000, **0 disables**).
  Measured reach at N = 5 / 8 / 12 / 20: **14.0 / 8.2 / 5.0 / 2.5 MB**, counting the
  `personal_impacts` rows that are no longer derived as well as the judgments
  themselves. Both skip paths set `needs_judgment = 0`; without that they would
  re-queue forever and manufacture the very rows the cleanup is removing.
- **F3's product cost is documented in the code**, not hidden: past the cap the
  cluster's judgment stops tracking new evidence. 8.2 MB against 281 MB of judgments
  is roughly 3 %, which is a poor trade, so the cap is configurable and can be
  switched off.

## Structural work

- **HTTP boundary.** `http_api.py` held 110 `if`/`elif` branches and 49 `/api` routes
  across four `do_*` methods. These are now a **61-entry declarative route table**
  (GET 29 / POST 26 / PUT 4 / DELETE 2, list order is priority) supporting exact,
  prefix and prefix-with-suffix matching, with an unknown-method/path 404 fallback.
  `do_GET` is 9 lines, `do_POST` 15, `do_PUT` 11, `do_DELETE` 11. The table is
  validated at `create_server` time so a misspelled endpoint fails loudly instead of
  silently 404-ing. All five security headers and the constant-time token comparison
  are unchanged, and the deliberate ordering difference between `do_GET` (static
  assets before auth) and `do_POST` (auth first) was preserved. Equivalence was
  checked by a harness that reproduces the old branch chain independently and compares
  the handler and extracted parameters for every route, rather than by asking the new
  code to confirm itself.
- **`judgments.py`** (1247 lines) became a 61-line façade plus
  `judgment_models` / `judgment_bundle` / `judgment_validation` / `judgment_local`.
  The split is an AST-driven, zero-rewrite slice: 20 top-level definitions and
  51,567 bytes were compared character by character and are unchanged, and every
  public name still imports from the original path.

## Robustness change, and its unsettled evidence

An oversized request body (> 64 KiB) used to be rejected **without reading it**. It is
now **drained with a bound (1 MiB) and a 5-second read timeout** before the 400 is
written.

This is recorded as a trade rather than an improvement, because the evidence is
one-sided:

- Before the change the path did not read at all, so it could not block.
- After the change, a peer that declares far more than it sends keeps a worker thread
  for up to `DRAIN_TIMEOUT_SECONDS`. Measured: declaring 5 MB and sending nothing
  yields the 400 after **5.00 s**; declaring 5 MB and sending exactly 1 MiB yields it
  in **0.01 s**, which is what demonstrates the bound actually stops the read.
- The failure this was meant to address — a client losing the 400 with
  `RemoteDisconnected` when the connection is reset with unread bytes buffered — was
  observed once under full-suite load but **could not be reproduced deterministically**.
  A controlled experiment that replaced the drain with a no-op still delivered a clean,
  readable 400 at every body size tried.

The change is kept because the blocking path requires a client that lies about its
content length, and this server binds to loopback only, is token-gated, and serves a
single local window. **The RST symptom is not claimed as fixed.** What is pinned by
tests is the bounded behaviour, and the test docstrings state explicitly that they
cannot distinguish draining from not draining.

## Caveats found, and what happened to them

- **Retention layer A was ineffective for ~80 % of its rows — fixed in a follow-up
  commit.** 77,231 of 96,613 `external_items` rows have `published_at IS NULL`, and 537
  more store a raw RSS RFC-822 date. Layer A deleted on `published_at < cutoff`; a NULL
  comparison is never true and `'T' > '2'` lexicographically, so neither group could
  ever be deleted. There was no loss at the time — the table spans only 39 days and the
  window is 60 — but from about 2026-10-05 the table (55.7 MB, the third largest) would
  have begun accumulating permanently.
  The condition is now
  `COALESCE(CASE WHEN published_at LIKE '____-__-__T%' THEN published_at END, first_seen_at) < ?`,
  so a valid ISO publish date still wins and anything else falls back to when the item
  was first seen. RFC-2822 dates are **not** parsed back into the row and NULLs are
  **not** backfilled: that would fabricate a publication date, and `first_seen_at` is
  the honest fallback. Ingestion needed no change — `normalize_published_at()` already
  existed and all three parse paths already called it, so the unparseable rows are
  legacy data, not an ongoing defect.
  Measured effect on the real database: **0 extra rows deleted today** (304 either way),
  so this is future-proofing only. Cost: the new predicate cannot use
  `idx_external_items_published` and degrades to a full scan, 97.0 ms → 107.8 ms; with
  the index present it would be 0.3 ms → ~122 ms. The absolute cost is 0.12 s once a day.
  A correction worth recording: **"normalising the legacy rows would restore the index"
  is false** — 80 % of rows are NULL and always take the fallback, so `COALESCE` remains
  and the index stays unusable. Recovering it would require materialising the predicate
  into a column, which means changing the write path.
- **The five hot-path indexes did not exist in the live database.** See the deployment
  note below; the cause is a stale installed build, not a code fault.
- Two suites that claim to pin retention behaviour were found to be vacuous during
  this round and were repaired: an audit-log assertion that could never fail, and an
  idempotency assertion that survived a mutation deleting one row per layer. Both were
  caught by deliberately mutating the source and checking that the tests turn red — a
  check that coverage alone cannot provide.

## Deployment note: the installed build lags the source

The live database is missing all five hot-path indexes declared in `database.py`
(`idx_external_items_source`, `idx_external_items_published`,
`idx_notification_log_cluster_created`, `idx_judgment_jobs_finished`,
`idx_event_cluster_items_item`). This is not a defect: when the database already
exists, `application.py` takes the `else` branch and calls `database.initialize()` on
**every** startup, and those indexes are unconditional `CREATE INDEX IF NOT EXISTS`
statements. The only consistent explanation is that **the installed program predates
the commit that introduced them**, so those statements have never run against the live
database.

Consequences: those hot paths are still full table scans, and **none of this round's
work is active in the running program** — not the retention layers, not the route
table, not the layer-A fix, not the added tests. A rebuilt and reinstalled package is
required; the first start after that creates the indexes once (about 5 s, about 14 MB).

## Not verified

- **No package was built.** This round changed no build inputs and produced no binary,
  so there is no artifact hash to record and no packaged smoke run. The v1.0 artifact
  remains the newest build.
- **No desktop acceptance pass.** The window, the tray, the first-run tutorial and the
  "Tell YuanJian" walkthrough were not exercised by hand.
- **No write was made to the live database.** Every retention measurement ran against
  a copy; the live database was opened read-only throughout.
- **The RST behaviour above is not reproduced**, as described.

## Privacy gate

    python tools/privacy_scan.py --committed

reported `128 files, safe=True, blocked=0, findings=0`. Note that `--committed` reads
the git **index** (`git ls-files`), not `HEAD`, so staging the files is enough to bring
newly added modules under the gate — running it before staging would have left this
round's six new files unscanned while still reporting a pass.

Scanning the whole working tree instead reports 36 findings; all of them lie in
ignored directories (`build-artifacts/`, a build virtualenv, and a materials folder
listed in `.gitignore`), so none are in the publication tree. That is expected, and it
is why the release gate runs in `--committed` mode.

## Repository note

No history rewrite was performed in this round. No commit was amended, and no remote
was pushed as part of producing this record.
