"""Schedule management commands."""

import click

from ..client import TrinityClient
from ..output import format_output


@click.group()
def schedules():
    """Manage agent schedules."""
    pass


@schedules.command("list")
@click.argument("agent")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="json", help="Output format")
def list_schedules(agent, fmt):
    """List schedules for an agent."""
    client = TrinityClient()
    data = client.get(f"/api/agents/{agent}/schedules")
    if fmt == "table" and isinstance(data, list):
        rows = [
            {
                "id": s.get("id", ""),
                "skill": s.get("skill_name", ""),
                "cron": s.get("cron_expression", ""),
                "enabled": s.get("enabled", ""),
            }
            for s in data
        ]
        format_output(rows, fmt)
    else:
        format_output(data, fmt)


@schedules.command("trigger")
@click.argument("agent")
@click.argument("schedule_id")
def trigger_schedule(agent, schedule_id):
    """Trigger a schedule immediately."""
    client = TrinityClient()
    data = client.post(f"/api/agents/{agent}/schedules/{schedule_id}/trigger")
    click.echo(f"Triggered schedule {schedule_id} on '{agent}'")
