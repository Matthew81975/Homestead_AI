# Local Codex Windows Smoke Test — HCS 0.11.0

Use a disposable test workspace and record the result of each check. Do not use
a repository with uncommitted work for approval or crash testing.

1. **Startup** — Launch HCS normally. Expected: the HCS server becomes ready,
   the Local Codex tab opens, and Worker changes to `running` without a console
   window.
2. **Migration receipt** — On the first launch, inspect
   `data\local_codex\migration\receipt.json`. Expected: it is valid JSON, names
   the selected standalone source (or records that none was found), contains no
   credentials, and the old folder is unchanged.
3. **Workspace selection** — Press Refresh and select a workspace. Expected: all
   enabled migrated registrations appear once and disabled registrations do not.
4. **LM Studio task** — With LM Studio available, submit a short read-only task.
   Expected: task state advances from working to completed and ordered activity
   appears in the live log.
5. **Cloud-to-local fallback** — Make the preferred cloud route unavailable and
   submit a safe task. Expected: the log shows the cloud provider, fallback, and
   `local-lm-studio` without exposing prompt secrets or credentials.
6. **Approval** — Run a task that reaches task review. Expected: Approve and Deny
   enable only for the current request; a stale response is rejected; neither
   choice silently grants commit or push authority.
7. **Stop and Resume** — Stop an active task, wait for its interrupted/blocked
   state, then press Resume. Expected: the journal is retained and execution
   resumes in the same workspace.
8. **Crash and restart** — End the worker process in Task Manager. Expected: HCS
   stays open, reports the crash, and automatically restarts only within the
   configured retry limit.
9. **Tray behavior** — Hide the standard HCS window to the tray during a task.
   Expected: the task and worker continue running, and reopening HCS shows the
   accumulated log.
10. **Full exit cleanup** — Choose Exit HCS (free memory). Expected: Local Codex,
    its model/tool descendants, and the HCS server disappear from Task Manager.
11. **Log controls** — Verify Follow, Pause Scroll, case-insensitive search,
    Clear View, and Open Log Folder. Expected: Clear View leaves the files in the
    log folder intact.
12. **HCS update/restart** — Trigger Check for Updates and accept restart.
    Expected: the worker stops before update, HCS restarts at 0.11.0, local
    `config.json` and `data\local_codex` remain intact, and the tab reconnects.
