from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import typer

from jira_codex_agent.config import Settings

app = typer.Typer(help="Control the Jira Codex background agent.", no_args_is_help=True)


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def _send(command: str) -> dict[str, Any]:
    path = _settings().socket_path
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except (FileNotFoundError, ConnectionRefusedError):
        raise typer.BadParameter("daemon is not running; use `jira-codex-agent start`") from None
    writer.write((json.dumps({"command": command}) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


def _call(command: str) -> dict[str, Any]:
    return asyncio.run(_send(command))


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
