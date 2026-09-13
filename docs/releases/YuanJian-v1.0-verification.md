# YuanJian v1.0 verification

Verified on 2026-09-13 on Windows with the packaged pywebview desktop build.

## Scope

This record covers `main` at commit `347ea7dc26af05dc5383aff762df7e2b6bcbc4d9` (version
`1.0.0`, reported by `src/yuanjian_app/__init__.py` and asserted by
`tests/test_build_config.py`).

Unlike v0.6–v0.9, this entry was produced by rebuilding the package from the recorded
commit and exercising that build through the automated smoke harness. No manual
desktop acceptance pass was performed; the gap is listed under "Not verified" below
rather than being inferred from the automated results.

## Automated verification

- Full suite: **207 tests passed in 31.234 seconds** with `ResourceWarning` treated as
  an error (`unittest discover -s tests`).
- Privacy scan of the exact committed publication tree (109 files exported from
  `git ls-files`): `safe=True blocked=0 findings=0`.
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

## Not verified

- Desktop acceptance was not performed in this pass: opening the native window, tray
  behaviour after closing the window, the first-open three-step tutorial, and the
  "Tell YuanJian" walkthrough were not exercised by hand. The smoke harness runs
  headless (`YUANJIAN_HEADLESS=1`, `--background`), so it covers the loopback API and
  the served assets but not the interactive window.

## Artifact

- Path: `build-artifacts/v1.0-dist/YuanJian/YuanJian.exe` (build output; not committed)
- Size: 6,762,739 bytes
- SHA-256: `AF8B5A3F073DAFDDC0CE8A5CA4FB261CF8B4D6FA7F2AE9CD1571575485E65C7A`
- Whole onedir tree: 182 files, 42.3 MB
- Built 2026-09-13 from the commit above with PyInstaller 6.21.0 on Python 3.14.5,
  `--clean`, dependencies as pinned in `build/build_windows.ps1`.

`dist/` is git-ignored, so no binary is published with this repository. Rebuild with
`powershell -ExecutionPolicy Bypass -File build\build_windows.ps1` and confirm the hash
above only if you need to reproduce this exact artifact.

## Known limitation of the privacy gate

`tools/privacy_scan.py` detects a Windows absolute path only when it is written with
backslash separators. The same path written with forward slashes is not detected.

Verified on 2026-09-13 against two files in an isolated directory: the file holding the
backslash form was reported as a sensitive-content pattern, the file holding the
forward-slash form was not reported.

A forward-slash local path did reach public history once before, which is why this
limitation is recorded here rather than left implicit. Until the pattern accepts both
separators, treat a clean scan as necessary but not sufficient, and review path-like
strings by hand before publishing.

## Repository note

The public history was rewritten and the repository was recreated on 2026-09-13 to
purge a leaked local path from earlier commits. Commit hashes from before that date no
longer resolve; the hashes recorded in this document resolve against the current
history only.
