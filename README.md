# Jira Codex Agent

A low-overhead, event-driven macOS daemon that picks assigned Jira work, creates an isolated Git worktree, runs Codex, and leaves completed changes waiting for human review.

The agent deliberately does **not** push branches, open pull requests, transition Jira issues, or merge code. Those actions stay behind human review.

## MVP flow

```text
Jira queue → Git worktree → Codex app-server → local review queue → quota check
     ↑                                                    │
     └──── sleep 10 min when idle / until reset if low ───┘
```

- Jira Cloud REST API v3 using API-token basic authentication
- Codex app-server with a workspace-write sandbox and human approval requests
- short-lived `codex app-server` process for the five-hour quota snapshot
- SQLite state with WAL mode
- Unix-domain socket for local CLI control
- `launchd` template for login startup

## Requirements

- macOS, Python 3.12+, Git, and an authenticated Codex CLI
- a clean local clone of the repository the agent should modify
- Jira Cloud email and API token

## Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env`, then validate the foreground process first:

```bash
.venv/bin/jira-codex-agent start --foreground
```

In another terminal:

```bash
.venv/bin/jira-codex-agent status
.venv/bin/jira-codex-agent tasks
.venv/bin/jira-codex-agent review
.venv/bin/jira-codex-agent pause
.venv/bin/jira-codex-agent resume
```

## launchd

Stop any daemon started from the app, then install the launch agent so the daemon starts at
login and restarts after a crash:

```bash
.venv/bin/jira-codex-agent install-service
```

It fills in [macos/com.jira-codex-agent.plist](macos/com.jira-codex-agent.plist) with this
virtualenv's Python and project directory. Once installed, the app's **Start daemon** button uses launchd.

## Unattended operation

The daemon is designed to work through the queue while nobody is at the Mac:

- **Blocked issues are not started.** An issue with an unfinished "is blocked by" / "depends on" link,
  a *Flagged* impediment, a status containing "Blocked" or a `blocked` label becomes `blocked`, and
  returns to the queue on the sync after the blocker is done.
- **Approvals never hang.** Requests that nobody answers within `JCA_APPROVAL_TIMEOUT_SECONDS`
  (15 min) are declined; Codex is told it runs unattended and should report instead of looping.
- **Codex's own verdict counts.** The final message is constrained by a JSON output schema. Only
  `COMPLETED` goes to *Needs review*; `PARTIAL`, `BLOCKED_*` and `NEEDS_HUMAN_REVIEW` go to *Needs you*.
- **Failures retry, then stop.** Crashes, disconnects and timeouts resume the same session with
  back-off up to `JCA_MAX_TASK_ATTEMPTS`, then become `failed`. A waiting retry never blocks other issues.
- **The Mac stays awake** (`caffeinate -i`) while a task runs or a quota reset is under six hours away.
  Closing the lid on battery still sleeps it.
- **Notifications** report tasks that are ready, need you, or failed.
- **History expires.** Logs of reviewed/skipped tasks and archived queue rows are pruned after
  `JCA_HISTORY_RETENTION_DAYS`; each task keeps at most `JCA_MAX_EVENTS_PER_TASK` log events.

### Undoing wrong work

| Command | App action | Effect |
|---|---|---|
| `retry KEY --note "…"` | Retry from scratch… | Remove worktree, the issue's local branch (including a renamed `feature/KEY-…`), logs and Codex thread; queue again with your note in the prompt |
| `skip KEY` | Discard and skip… | Same cleanup; never picked up again until `unskip KEY` |
| `delete KEY` | Delete task… | Same cleanup and forget the task; it returns if it still matches the JQL |
| `prune-history` | — | Apply the retention window now |

Select a task in the dashboard to see its Jira description, blockers, Codex report and test results.

The daemon logs to the path in `JCA_LOG_PATH`. It polls Jira only after the configured idle interval. While Codex runs it waits on the child process, and it reads quota only after successful work.

## Claude plan and review (optional)

Codex has a smaller context window, so an authenticated [Claude Code](https://claude.com/claude-code) CLI can
do the wide reading around it. Enable either pass in **Settings → Claude** or with `JCA_CLAUDE_PLAN` / `JCA_CLAUDE_REVIEW`:

```text
Claude plan (read-only) → Codex implements → COMPLETED? → Claude review (read-only)
                                                 ↑                    │ REQUEST_CHANGES
                                                 └── Codex fixes ─────┘ up to JCA_CLAUDE_REVIEW_ROUNDS
```

- Claude runs `claude -p` in the task worktree limited to `Read`, `Grep`, `Glob` and read-only git commands; it never edits files.
- The plan is added to the Codex prompt and kept for resumed runs.
- Blocker or major findings go back to the same Codex session. If they remain after the allowed rounds, the task goes to *Needs you*.
- Before every pass the daemon reads your Claude plan's limits with `claude -p /usage` (no model request). When the
  remaining 5-hour or weekly share is at or below `JCA_CLAUDE_QUOTA_REMAINING_THRESHOLD` /
  `JCA_CLAUDE_QUOTA_WEEKLY_REMAINING_THRESHOLD`, the pass is skipped so the rest stays for your own Claude work.
  The dashboard shows Claude's quota next to Codex's.
- If Claude is missing, low on quota or fails, Codex continues alone and the event appears in the task log.

## Native macOS UI

The SwiftUI app provides both a menu-bar controller and a full dashboard. It shows the current issue, queue, review and failure counts, task worktrees, and supports pause/resume. It communicates only through the daemon's local Unix socket.

Build the signed local app bundle:

```bash
macos/JiraCodexAgentUI/build-app.sh
open 'macos/JiraCodexAgentUI/dist/Jira Codex Agent.app'
```

Use **Accounts & Settings** in the app to configure Jira URL, email, API token, JQL, repository, and Codex CLI. The Jira token is written to `~/.config/jira-codex-agent/config.env` with file mode `0600`; environment variables still take precedence. The Codex sign-in button starts the official browser-based `codex login` flow and credentials remain managed by Codex itself.

The app expects the default socket at `~/.local/share/jira-codex-agent/agent.sock`. If `JCA_SOCKET_PATH` is customized, enter the same path under Advanced. Restart the daemon after saving configuration. The **Start daemon** button uses the `com.jira-codex-agent` launchd service, so install the launch agent first if you want that button to work.

The UI requires macOS 13 or newer. Its source is a standalone Swift package under `macos/JiraCodexAgentUI`, so it can also be opened directly in Xcode.

### Codex approvals

The dashboard polls pending approvals every two seconds and shows the task, command,
working directory, reason, and full request details. Choose **Approve** or **Decline**;
requests are never automatically accepted. Command/file approvals apply to one request;
explicit permission grants apply only to the current turn, never the whole session.
Existing Codex execution-policy rules still apply to ordinary sandboxed commands.
Human wait time does not count against the task execution timeout, and the scheduler
does not start another task while waiting. Closing the dashboard leaves the request
pending; stopping the daemon cancels pending approvals and preserves the worktree and
thread for resume. Restarted threads may ask for approval again. Unknown interactive
request types fail explicitly without silently granting access.

After upgrading, reopen the rebuilt application and restart the daemon. There is no
fallback to the noninteractive runner if app-server fails. This integration follows
the [official app-server protocol](https://learn.chatgpt.com/docs/app-server).

## Safety and behavior

- Every Jira issue gets branch `codex/<issue-key>` and its own worktree.
- Codex sessions reporting `COMPLETED` become `review`; other reports become `needs_human`; failures retry and are finally retained as `failed` with diagnostics.
- Quota lookup failure is non-fatal. The quota protocol is versioned with the locally installed Codex CLI, so the agent reports it as unavailable and continues conservatively.
- Set `JCA_DRY_RUN=true` to inspect queue discovery without creating worktrees or invoking Codex.

Run tests with `pytest`.
