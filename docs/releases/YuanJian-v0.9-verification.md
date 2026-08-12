# YuanJian v0.9 verification

Verified on 2026-08-12 on Windows 10 with the packaged pywebview desktop build.

## User experience

- The primary navigation contains only Action Home, Tell YuanJian, and Settings.
- Action Home states what to do before reasons or technical evidence and shows no raw news.
- A submitted personal fact returns one plain-language action followed by a Chinese risk level.
- Positive cash events such as salary arrival remain low risk; missing salary, urgent medical cost, and overdue events can still escalate.
- First launch shows a three-step tutorial. The same tutorial can be reopened from Settings.
- Closing the window removes the window while the process and loopback API keep running in the tray.

## Automated verification

- Full suite: 159 tests passed in 52.739 seconds with `ResourceWarning` treated as an error.
- JavaScript syntax checks and `git diff --check`: passed.
- Packaged smoke: loopback only, local assets only, Action Home default, local cognition fallback, second-instance rejection, and safe shutdown all passed.
- Privacy scan of the exact committed publication tree: `safe=True blocked=0 findings=0`.

## Desktop acceptance

The packaged desktop application was exercised with an isolated data directory. The official local database and the already-running older version were not modified.

- The first-open tutorial displayed all three steps and could be skipped.
- Action Home loaded as the default with the monitoring-coverage warning stated honestly.
- Tell YuanJian was reachable in one click and accepted the built-in salary-arrival example.
- Salary arrival returned: verify the amount and receipt, rebuild the cash buffer, and avoid new non-essential spending; risk was Low.
- Settings exposed the persistent tutorial, and Reopen Tutorial displayed the onboarding overlay again.
- Closing the native window hid it while the process remained alive and the loopback home endpoint still returned HTTP 200.
- The acceptance process exited cleanly through the authenticated shutdown endpoint.

## Artifact

- Path: `outputs/YuanJianApp-v0.9/YuanJian.exe`
- Size: 6,618,628 bytes
- SHA-256: `51064334F2A3AAFA5B0799F53183D54F4D8A822A035F19B8026C4D9725214B42`
- The previous package remains available for rollback until final cleanup.
