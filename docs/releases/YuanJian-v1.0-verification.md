# YuanJian v1.0 verification

Verified on 2026-09-13 on Windows with the packaged pywebview desktop build.

## Scope

This record covers `main` at commit `b939655071179d06e971826afadccf45a4f8080c`
(version `1.0.0`, reported by `src/yuanjian_app/__init__.py` and asserted by
`tests/test_build_config.py`). The rebuild and the smoke run below were performed on
that revision; this document itself was committed immediately afterwards and changes
no code.

Unlike v0.6–v0.9, this entry was produced by rebuilding the package from the recorded
commit and exercising that build through the automated smoke harness. No manual
desktop acceptance pass was performed; the gap is listed under "Not verified" below
rather than being inferred from the automated results.

## Automated verification

- Full suite: **220 tests passed in 40.244 seconds** with `ResourceWarning` treated as
  an error (`unittest discover -s tests`).
- Privacy scan of the exact committed publication tree (115 files, via
  `tools/privacy_scan.py --committed`): `safe=True blocked=0 findings=0`.
- Packaged smoke against the v1.0 build, all checks passed:
  - single listener bound to `127.0.0.1` (no non-loopback listener)
  - local database created under an isolated data directory
  - home page returned HTTP 200 with `data-view="today"` and the `/js/app.js` module entry
  - no legacy `data-view="world"` entry and no `https://` resource in the home page
  - `/js/views/today.js` served by the packaged build
  - cognition run with no API token fell back to the local provider (`provider=local`)
  - a second packaged instance was rejected
  - authenticated safe shutdown completed (`status=shutting_down`)

## Behaviour changes in this release

- **E1 is now hard-capped at L3.** `_alert_level()` previously graded on score alone, so
  E1 could reach L4. Because `map_judgment()` auto-confirms L4 candidates, a
  single-source clue could enter the immutable forecast ledger without a human choosing
  a probability, and it disappeared from the pending-candidate list at the same time.
  `impacts.py` now caps the level when the evidence weight is at or below E1, and treats
  an unknown or missing grade the same way. This closes the gap between the code and the
  boundary stated in `README.md` and `PRIVACY.md`.
- Four stale test fixtures were corrected; no production behaviour changed with them:
  the `RecordingScheduler` double now accepts `timeout`, `_base_result()` supplies
  `personal_action`, and the risk-dashboard wording assertion matches current output.
- **HTTP boundary hardening.** Every response now carries `Content-Security-Policy`
  (including `object-src`, `base-uri`, `form-action`, `frame-ancestors`),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy`. The session token is
  compared with `hmac.compare_digest`, and the frontend removes it from the address bar
  as soon as it is read. `PRIVACY.md` states the token's actual boundary: it stops
  off-machine and cross-origin access, not other local processes.
- **Diagnosability.** A rotating log file is written under
  `%LOCALAPPDATA%\YuanJian\logs`; previously the packaged build had no console and no
  handler, so log records went nowhere. Failed scheduled tasks now record a sanitised,
  path-redacted message alongside the exception type.
- **Performance.** Repeated text-feature extraction during clustering is cached, and the
  candidate-cluster query no longer selects unused columns. The scheduler computes the
  next due time from the moment a task actually finishes, so a task that overruns its
  own interval no longer triggers an immediate catch-up burst.
- **Build inputs.** Dependencies moved into `requirements.txt` / `requirements-build.txt`
  and the build script reads them, so there is one place to bump a version. Line endings
  are pinned by `.gitattributes`. CI runs the suite and the privacy gate on every push.

## Not verified

- Desktop acceptance was not performed in this pass: opening the native window, tray
  behaviour after closing the window, the first-open three-step tutorial, and the
  "Tell YuanJian" walkthrough were not exercised by hand. The smoke harness runs
  headless (`YUANJIAN_HEADLESS=1`, `--background`), so it covers the loopback API and
  the served assets but not the interactive window.

## Artifact

- Path: `build-artifacts/v1.0-dist/YuanJian/YuanJian.exe` (build output; not committed)
- Size: 6,814,506 bytes
- SHA-256: `C103C4C727B4A52738D63263B345A81040C3ACCAD50F8AEF8FA197DE3D3EBF6E`
- Whole onedir tree: 182 files, 42.4 MB
- Built 2026-09-13 from the commit above with PyInstaller 6.21.0 on Python 3.14.5,
  dependencies as pinned in `requirements-build.txt`.

`dist/` is git-ignored, so no binary is published with this repository. Rebuild with
`powershell -ExecutionPolicy Bypass -File build\build_windows.ps1`; the hash above
reproduces only if the pinned dependency versions and the Python minor version match.

## Privacy gate

`tools/privacy_scan.py` detects a Windows absolute path written with either separator.
Until 2026-09-13 it matched backslash separators only, which is how a forward-slash
local path reached public history while the scan still reported `safe=True`. Both forms
are now covered, and two regression tests pin that
(`test_scanner_catches_backslash_absolute_path` and
`test_scanner_catches_forward_slash_absolute_path`). `test_committed_tree_scan_is_clean`
runs the gate against the real committed tree as part of the suite, and CI runs it on
every push.

Before publishing anything, run:

    python tools/privacy_scan.py --committed

## Repository note

The public history was rewritten and the repository was recreated on 2026-09-13 to
purge a leaked local path from earlier commits. Commit hashes from before that date no
longer resolve; the hashes recorded in this document resolve against the current
history only.

`v1.0` tags the commit that adds this record. The build described above was produced
from its parent, which differs from the tag only by this documentation, so the artifact
matches the tagged tree's code exactly. The packaged onedir build is attached to that
tag's release, so a new build can be obtained without a local toolchain.
