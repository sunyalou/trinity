"""Health and monitoring commands."""

import click

from ..client import TrinityClient
from ..output import format_output


@click.group()
def health():
    """Fleet and agent health monitoring."""
    pass


@health.command("fleet")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="json", help="Output format")
def fleet_health(fmt):
    """Show fleet-wide health status."""
    client = TrinityClient()
    data = client.get("/api/monitoring/status")
    format_output(data, fmt)


@health.command("agent")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="json", help="Output format")
def agent_health(name, fmt):
    """Show health status for a specific agent."""
    client = TrinityClient()
    data = client.get(f"/api/monitoring/agents/{name}")
    format_output(data, fmt)
