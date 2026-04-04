"""Tag management commands."""

import click

from ..client import TrinityClient
from ..output import format_output


@click.group()
def tags():
    """Manage agent tags."""
    pass


@tags.command("list")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def list_tags(fmt):
    """List all tags in use."""
    client = TrinityClient()
    data = client.get("/api/tags")
    format_output(data, fmt)


@tags.command("get")
@click.argument("agent")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def get_agent_tags(agent, fmt):
    """Get tags for a specific agent."""
    client = TrinityClient()
    data = client.get(f"/api/agents/{agent}/tags")
    format_output(data, fmt)
