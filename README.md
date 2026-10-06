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

Replace `__PROJECT_DIR__` and `__PYTHON__` in [macos/com.jira-codex-agent.plist](macos/com.jira-codex-agent.plist), then:

```bash
cp macos/com.jira-codex-agent.plist ~/Library/LaunchAgents/com.jira-codex-agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jira-codex-agent.plist
```

The daemon logs to the path in `JCA_LOG_PATH`. It polls Jira only after the configured idle interval. While Codex runs it waits on the child process, and it reads quota only after successful work.

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
- Successfully completed Codex sessions become `review`; failures are retained as `failed` with diagnostics.
- Quota lookup failure is non-fatal. The quota protocol is versioned with the locally installed Codex CLI, so the agent reports it as unavailable and continues conservatively.
- Set `JCA_DRY_RUN=true` to inspect queue discovery without creating worktrees or invoking Codex.

Run tests with `pytest`.
