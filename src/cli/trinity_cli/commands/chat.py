"""Chat and log commands."""

import click

from ..client import TrinityClient
from ..output import format_output


@click.command("chat")
@click.argument("agent")
@click.argument("message")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def chat_with_agent(agent, message, fmt):
    """Send a message to an agent.

    Example: trinity chat my-agent "What is the status?"
    """
    client = TrinityClient()
    data = client.post(f"/api/agents/{agent}/chat", json={"message": message})
    if fmt == "json":
        format_output(data, fmt)
    else:
        # In table mode, just print the response text
        response = data.get("response", data) if isinstance(data, dict) else data
        click.echo(response)


@click.group("chat-history")
def chat_history_group():
    """Chat history commands."""
    pass


@click.command("history")
@click.argument("agent")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def chat_history(agent, fmt):
    """Get chat history for an agent."""
    client = TrinityClient()
    data = client.get(f"/api/agents/{agent}/chat/history")
    format_output(data, fmt)


@click.command("logs")
@click.argument("agent")
@click.option("--tail", default=50, help="Number of log lines")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table", help="Output format")
def logs(agent, tail, fmt):
    """View agent container logs.

    Example: trinity logs my-agent --tail 100
    """
    client = TrinityClient()
    data = client.get(f"/api/agents/{agent}/logs", params={"tail": tail})
    if fmt == "json":
        format_output(data, fmt)
    else:
        # Print logs as plain text
        if isinstance(data, dict) and "logs" in data:
            click.echo(data["logs"])
        elif isinstance(data, str):
            click.echo(data)
        else:
            format_output(data, fmt)
