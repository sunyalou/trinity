"""Trinity CLI — main entry point.

Usage:
    trinity init                    # Set up and authenticate
    trinity login                   # Log in to an instance
    trinity agents list             # List agents
    trinity chat my-agent "hello"   # Chat with an agent
    trinity logs my-agent           # View agent logs
"""

import click

from . import __version__
from .commands.agents import agents
from .commands.auth import init, login, logout, status
from .commands.chat import chat_history, chat_with_agent, logs
from .commands.health import health
from .commands.schedules import schedules
from .commands.skills import skills
from .commands.tags import tags


@click.group()
@click.version_option(version=__version__, prog_name="trinity")
def cli():
    """Trinity — Autonomous Agent Orchestration Platform CLI.

    Get started:

        trinity init          Configure instance and log in

        trinity agents list   List your agents

        trinity chat <agent> "message"   Chat with an agent
    """
    pass


# Auth commands (top-level)
cli.add_command(init)
cli.add_command(login)
cli.add_command(logout)
cli.add_command(status)

# Resource commands (groups)
cli.add_command(agents)
cli.add_command(health)
cli.add_command(skills)
cli.add_command(schedules)
cli.add_command(tags)

# Standalone commands
cli.add_command(chat_with_agent)
cli.add_command(chat_history, name="history")
cli.add_command(logs)


def main():
    cli()


if __name__ == "__main__":
    main()
