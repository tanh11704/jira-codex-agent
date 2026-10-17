from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from jira_codex_agent.config import Settings

app = typer.Typer(help="Control the Jira Codex background agent.", no_args_is_help=True)


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def _send(command: str, **extra: Any) -> dict[str, Any]:
    path = _settings().socket_path
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except (FileNotFoundError, ConnectionRefusedError):
        raise typer.BadParameter("daemon is not running; use `jira-codex-agent start`") from None
    writer.write((json.dumps({"command": command, **extra}) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


def _call(command: str, **extra: Any) -> dict[str, Any]:
    return asyncio.run(_send(command, **extra))


def _checked(command: str, **extra: Any) -> dict[str, Any]:
    response = _call(command, **extra)
    if response.get('error'):
        typer.echo(response['error'])
        raise typer.Exit(1)
    return response


@app.command()
def start(foreground: bool = typer.Option(False, "--foreground", "-f")) -> None:
    """Start directly, or ask launchd to start the installed service."""
    if foreground:
        from jira_codex_agent.main import main

        main()
        return
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.jira-codex-agent"],
        check=False,
    )
    if result.returncode:
        raise typer.Exit(result.returncode)
    typer.echo("Agent start requested.")


@app.command()
def status() -> None:
    response = _call("status")
    state = "PAUSED" if response.get("paused") else "RUNNING"
    typer.echo(f"Status: {state}")
    current = response.get("current")
    typer.echo(f"Current: {current['issue_key']} — {current['summary']}" if current else "Current: idle")


def _print_tasks(command: str) -> None:
    tasks = _call(command).get("tasks", [])
    if not tasks:
        typer.echo("No tasks.")
        return
    for task in tasks:
        typer.echo(f"{task['issue_key']:<14} {task['state']:<10} {task['summary']}")


@app.command("tasks")
def tasks_command() -> None:
    _print_tasks("tasks")


@app.command()
def review() -> None:
    _print_tasks("review")


@app.command()
def pause() -> None:
    _call("pause")
    typer.echo("Agent paused after the current Codex process finishes.")


@app.command()
def resume() -> None:
    _call("resume")
    typer.echo("Agent resumed.")


@app.command('resume-task')
def resume_task(issue_key: str) -> None:
    response = _call('resume_task:' + issue_key.upper())
    if response.get('error'):
        typer.echo(response['error'])
        raise typer.Exit(1)
    typer.echo('Task queued for recovery in its original worktree. Resume the agent if paused; dry-run must be disabled.')


@app.command()
def retry(issue_key: str, note: str = typer.Option('', '--note', '-n', help='Guidance added to the new attempt')) -> None:
    """Discard the task's worktree, branch and history, then queue it again from scratch."""
    response = _checked(f'discard:{issue_key.upper()}:retry', note=note)
    typer.echo(f"Queued {issue_key.upper()} again. Deleted branches: {', '.join(response['deletedBranches']) or 'none'}")


@app.command()
def skip(issue_key: str) -> None:
    """Discard the task's work and never pick it up automatically again."""
    _checked(f'discard:{issue_key.upper()}:skip')
    typer.echo(f'{issue_key.upper()} discarded and skipped. Use `unskip` to allow it again.')


@app.command()
def unskip(issue_key: str) -> None:
    _checked(f'unskip:{issue_key.upper()}')
    typer.echo(f'{issue_key.upper()} is back in the queue.')


@app.command()
def delete(issue_key: str) -> None:
    """Discard the task's work and forget it. It returns if it still matches the JQL."""
    _checked(f'discard:{issue_key.upper()}:delete')
    typer.echo(f'{issue_key.upper()} deleted.')


@app.command('prune-history')
def prune_history() -> None:
    """Apply the history retention window now."""
    typer.echo(f"Removed {_checked('prune_history')['removed']} log events.")


@app.command('install-service')
def install_service() -> None:
    """Install and start the launchd agent so the daemon survives crashes, logout and reboot."""
    try:
        _call('status')
        typer.echo('A daemon is already running outside launchd. Stop it first (`Stop` in the app), then rerun.')
        raise typer.Exit(1)
    except typer.BadParameter:
        pass
    project = Path(__file__).resolve().parents[3]
    template = (project / 'macos' / 'com.jira-codex-agent.plist').read_text()
    plist = template.replace('__PYTHON__', sys.executable).replace('__PROJECT_DIR__', str(project))
    target = Path('~/Library/LaunchAgents/com.jira-codex-agent.plist').expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', f'{domain}/com.jira-codex-agent'], check=False, capture_output=True)
    target.write_text(plist)
    result = subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=False)
    if result.returncode:
        raise typer.Exit(result.returncode)
    typer.echo(f'Installed {target}; launchd now starts the agent at login and restarts it if it crashes.')


@app.command()
def install_launchd(
    template: Path = typer.Option(Path("macos/com.jira-codex-agent.plist")),
) -> None:
    """Print safe installation instructions for the launchd template."""
    if not template.exists():
        raise typer.BadParameter(f"template not found: {template}")
    typer.echo("Edit the Python path and WorkingDirectory in the plist, then run:")
    typer.echo(f"  cp {template} ~/Library/LaunchAgents/com.jira-codex-agent.plist")
    typer.echo("  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jira-codex-agent.plist")
