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

### Agents per job (Codex, Claude Code, Antigravity)

**Settings → Agents** picks the agent and model for each job; the switches turn the optional jobs on
(`JCA_CLAUDE_PLAN`, `JCA_CLAUDE_REVIEW`, `JCA_CLAUDE_FRONTEND`, `JCA_CLAUDE_DECIDE`, names kept for existing configs):

| Job | Agents | Default | Settings |
|---|---|---|---|
| Plan (read-only, also classifies the issue) | Claude, Antigravity | Claude · Opus | `JCA_PLAN_AGENT`, `JCA_PLAN_MODEL`, `JCA_PLAN_EFFORT` |
| Backend (or the whole issue) | Codex, Claude Code, Antigravity | Codex · `JCA_CODEX_MODEL` / effort | `JCA_BACKEND_AGENT`, `_MODEL`, `_EFFORT` |
| Frontend | Antigravity, Claude Code, Codex | Antigravity · Gemini 4 Hydrogen (High) | `JCA_FRONTEND_AGENT`, `_MODEL`, `_EFFORT` |
| Review (read-only) | Claude, Antigravity | Claude | `JCA_REVIEW_AGENT`, `_MODEL`, `_EFFORT` |
| Decisions (read-only) | Claude, Antigravity | Claude | `JCA_DECIDE_AGENT`, `_MODEL`, `_EFFORT` |

An empty model uses the agent's default (`JCA_CLAUDE_MODEL`, `JCA_ANTIGRAVITY_MODEL`, `JCA_CODEX_MODEL`). Gemini 4 Hydrogen
(High) is the default for UI work because it ranks highest on WebDev Arena among the models an Antigravity plan offers and
uses less quota than XHigh.

With frontend routing on, the planner (or a short read-only pass) classifies each issue:

```text
frontend  → frontend agent implements → Needs review
backend   → backend agent implements → review / fixes in the same session → Needs review
fullstack → backend agent implements the server side → review / fixes → frontend agent builds the UI on top → Needs review
```

- Every coder gets the same task prompt (including **UI QUALITY** and **VISUAL DESIGN AND IMAGERY**), the same report schema
  with its `images` slots, local checks and decision rounds, and its session is resumed after interruptions and answers.
- Claude Code and Antigravity edit the worktree directly, without Codex's sandbox, and may run any command except
  history-changing git commands (`commit`, `push`, `reset`, `rebase`, `merge`, `checkout`, `switch`, `stash`; Antigravity
  also `clean`). Read-only jobs may only read the worktree and run read-only git commands. Coding is bounded by
  `JCA_MAX_TASK_SECONDS`.
- Antigravity runs `agy --print` headless with `--output-format stream-json`, a JSON schema and per-run permission rules;
  `JCA_ANTIGRAVITY_COMMAND` defaults to `~/.local/bin/agy`. Run `agy` once in Terminal to sign in.
- Fallbacks: Claude's reserved quota, a missing CLI, or Antigravity reporting quota/rate-limit/sign-in errors when it
  starts hands the work to the backend agent (or Codex). Frontend-only work by its own agent is reviewed by a human.
- Each task records who did each job (`agents`: agent, model, effort, tokens, passes); the task details show it under
  **Who did what**, e.g. `PLAN · Claude · Opus`, `BE · Codex · GPT-6.1 Sol · High`, `FE · Antigravity · Gemini 4 Hydrogen (High)`.

## Local checks (save agent tokens)

Full builds and test suites flood an agent's context with logs. Set the commands in **Settings → Prompt → Local checks**
(one per line, stored as `JCA_VERIFY_COMMANDS_BASE64`) and the daemon runs them itself in the worktree after Codex or
Claude reports COMPLETED:

- Agents are told not to run full builds/suites, only narrow checks (one test file, one type-check) while coding.
- Commands run in order through your login shell (same PATH as Terminal) and stop at the first failure.
- A failure goes back to the same agent session as a trimmed excerpt (error lines plus the log tail, about 6 KB at most)
  for up to `JCA_VERIFY_FIX_ROUNDS` (default 2) rounds; still failing → the report becomes PARTIAL and goes to *Needs you*.
- Passing commands are added to the report's tests. Checks run before Claude's review, so the review never reads a broken
  build, and again after every fix.
- Include setup in the list when a fresh worktree needs it, e.g. `pnpm install --frozen-lockfile`.
- A failure whose log reports per-test timeouts (e.g. Vitest's `Test timed out in 5000ms`) is rerun
  `JCA_VERIFY_FLAKY_RETRIES` times (default 1) before it is blamed, without spending a fix round; a rerun that
  passes is listed in the report as an unstable check instead of ending the task PARTIAL.
- A PARTIAL report whose `blockers` list is empty (only BLOCKED checks remain) is verified here like
  `sandbox_blocked_only`, and upgraded to COMPLETED when the checks pass.

### Recheck and dependency resume

- **Recheck without agent** (task menu, or `recheck:<KEY>` on the control socket): for a PARTIAL task whose code is
  done but whose checks timed out or were blocked. The daemon reruns the local checks and UI capture on the existing
  worktree with no agent turn; passing sends it on to review as COMPLETED, and only a real failure wakes the agent in its
  own session. Tasks with open questions must be answered instead.
- **Dependency resume:** a `BLOCKED_DEPENDENCY` task whose blockers name other issues (e.g. `TM-103`) is resumed
  automatically once all of them are Done in Jira and its own Jira links show no unfinished blocker. Its worktree is
  first rebased onto the fetched base with `--autostash`, keeping uncommitted work; if a file changed both in the task
  and on the base, nothing is touched and you are notified. Each set of dependencies triggers one automatic resume.

### UI screenshots by the daemon

Agents do not start dev servers, drive browsers or look at screenshots. For each changed screen and state they add an
item to `ui_captures` in their report: the built portal directory (e.g. `apps/teacher-web/dist`), the route,
localStorage entries and the API mocks it needs. After the local checks pass (they include the web build), the daemon
renders each item in headless Google Chrome at 360 and 1280 px, plus the declared main action focused:

- No dev server or backend: files come from the build, API calls from the mocks; unmocked API calls are listed.
- Horizontal overflow, partly overlapping controls and page errors fail like a check and go back to the agent.
  Fully contained controls (a show-password button inside an input) are not flagged.
- PNGs land in `.ui-evidence/auto/` and are listed in the report's `screenshots`, so only the reviewer opens them.
- `playwright-core` is installed once into `<data dir>/ui-capture` with npm; no browser download (it uses Chrome).
- If the capture cannot run, it is recorded as BLOCKED and the task continues; the human checks the UI.

Turn it off with `JCA_UI_CAPTURE=false` (**Settings → Prompt → UI screenshots**).

### Codex sandbox network

`JCA_CODEX_NETWORK_ACCESS` (default `false`, **Settings → Automation → Approvals**) opens sockets in Codex's
`workspace-write` sandbox. It is not needed for Docker-backed checks or UI screenshots, which the daemon runs itself;
turning it on lets the agent run them in its own context and spend tokens on their logs.

## Lively UI and images

The default prompt's **VISUAL DESIGN AND IMAGERY** rules ask for hierarchy, cards, icons and purposeful images instead of
plain text. Images come from repository assets, from free-licensed web images whose URL the agent actually opened, or
as a placeholder with a fallback plus an English generation prompt. Every slot is listed in the report's `images`
(placement, target path, aspect ratio, URL + license or prompt). The task detail shows them with **Copy prompt** and
**Copy path**, so you generate the image during review and save it at that path; no code change is needed. When a task finishes the daemon checks
every slot's file in the worktree (whatever source the agent claimed) and the app marks each one **Missing file** or
**File present**, re-checked live, so a slot flips to present as soon as you save the generated image. Custom prompts
saved earlier get this section appended automatically.

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
