"""Agent management commands."""

import click

from ..client import TrinityClient, TrinityAPIError
from ..output import format_output


@click.group()
def agents():
    """Manage agents."""
    pass


@agents.command("list")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def list_agents(fmt):
    """List all agents."""
    client = TrinityClient()
    data = client.get("/api/agents")
    if fmt == "table" and isinstance(data, list):
        # Slim down for table view
        rows = [
            {
                "name": a.get("name", ""),
                "status": a.get("status", ""),
                "template": a.get("template", ""),
                "type": a.get("type", ""),
            }
            for a in data
        ]
        format_output(rows, fmt)
    else:
        format_output(data, fmt)


@agents.command("get")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def get_agent(name, fmt):
    """Get agent details."""
    client = TrinityClient()
    data = client.get(f"/api/agents/{name}")
    format_output(data, fmt)


@agents.command("create")
@click.argument("name")
@click.option("--template", default=None, help="Template (e.g. github:Org/repo)")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def create_agent(name, template, fmt):
    """Create a new agent."""
    client = TrinityClient()
    payload = {"name": name}
    if template:
        payload["template"] = template
    data = client.post("/api/agents", json=payload)
    format_output(data, fmt)


@agents.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this agent?")
def delete_agent(name):
    """Delete an agent."""
    client = TrinityClient()
    client.delete(f"/api/agents/{name}")
    click.echo(f"Deleted agent '{name}'")


@agents.command("start")
@click.argument("name")
def start_agent(name):
    """Start an agent container."""
    client = TrinityClient()
    client.post(f"/api/agents/{name}/start")
    click.echo(f"Started agent '{name}'")


@agents.command("stop")
@click.argument("name")
def stop_agent(name):
    """Stop an agent container."""
    client = TrinityClient()
    client.post(f"/api/agents/{name}/stop")
    click.echo(f"Stopped agent '{name}'")


@agents.command("rename")
@click.argument("name")
@click.argument("new_name")
def rename_agent(name, new_name):
    """Rename an agent."""
    client = TrinityClient()
    client.put(f"/api/agents/{name}/rename", json={"new_name": new_name})
    click.echo(f"Renamed '{name}' -> '{new_name}'")
