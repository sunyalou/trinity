"""Authentication commands: login, logout, status, init."""

import click

from ..client import TrinityClient, TrinityAPIError
from ..config import clear_auth, get_instance_url, get_user, load_config, set_auth


@click.command()
@click.option("--instance", help="Trinity instance URL (e.g. https://trinity.example.com)")
def login(instance):
    """Log in to a Trinity instance with email verification."""
    url = instance or get_instance_url()
    if not url:
        url = click.prompt("Trinity instance URL")
    url = url.rstrip("/")

    client = TrinityClient(base_url=url, token="none")

    email = click.prompt("Email")

    # Request verification code
    try:
        client.post_unauthenticated("/api/auth/email/request", {"email": email})
    except TrinityAPIError as e:
        click.echo(f"Error requesting code: {e.detail}", err=True)
        raise SystemExit(1)

    click.echo(f"Verification code sent to {email}")
    code = click.prompt("Enter 6-digit code")

    # Verify code and get token
    try:
        result = client.post_unauthenticated("/api/auth/email/verify", {
            "email": email,
            "code": code,
        })
    except TrinityAPIError as e:
        click.echo(f"Verification failed: {e.detail}", err=True)
        raise SystemExit(1)

    token = result["access_token"]
    user = result.get("user")

    set_auth(url, token, user)
    name = user.get("name") or user.get("email") or user.get("username") if user else email
    click.echo(f"Logged in as {name}")


@click.command()
def logout():
    """Clear stored credentials."""
    clear_auth()
    click.echo("Logged out")


@click.command()
def status():
    """Show current login status and instance info."""
    url = get_instance_url()
    if not url:
        click.echo("Not configured. Run 'trinity init' or 'trinity login'.")
        return

    user = get_user()
    config = load_config()

    click.echo(f"Instance: {url}")
    if user:
        click.echo(f"User:     {user.get('email') or user.get('username')}")
        click.echo(f"Role:     {user.get('role', 'unknown')}")
    elif config.get("token"):
        click.echo("User:     (API key auth)")
    else:
        click.echo("User:     Not logged in")

    # Check connectivity
    try:
        client = TrinityClient(base_url=url, token=config.get("token", "none"))
        client.get_unauthenticated("/api/auth/mode")
        click.echo("Status:   Connected")
    except Exception:
        click.echo("Status:   Unreachable")


@click.command()
def init():
    """Set up Trinity CLI: configure instance, request access, and log in.

    One command to go from zero to authenticated.
    """
    url = click.prompt("Trinity instance URL", default="http://localhost:8000")
    url = url.rstrip("/")

    client = TrinityClient(base_url=url, token="none")

    # Verify instance is reachable
    try:
        client.get_unauthenticated("/api/auth/mode")
    except Exception:
        click.echo(f"Cannot reach {url}. Check the URL and try again.", err=True)
        raise SystemExit(1)

    click.echo(f"Connected to {url}")

    email = click.prompt("Email")

    # Request access (auto-approve endpoint)
    try:
        client.post_unauthenticated("/api/access/request", {"email": email})
        click.echo("Access granted")
    except TrinityAPIError as e:
        if e.status_code == 409:
            click.echo("Already registered")
        else:
            click.echo(f"Access request failed: {e.detail}", err=True)
            raise SystemExit(1)

    # Send verification code
    try:
        client.post_unauthenticated("/api/auth/email/request", {"email": email})
    except TrinityAPIError as e:
        click.echo(f"Error requesting code: {e.detail}", err=True)
        raise SystemExit(1)

    click.echo(f"Verification code sent to {email}")
    code = click.prompt("Enter 6-digit code")

    # Verify and get token
    try:
        result = client.post_unauthenticated("/api/auth/email/verify", {
            "email": email,
            "code": code,
        })
    except TrinityAPIError as e:
        click.echo(f"Verification failed: {e.detail}", err=True)
        raise SystemExit(1)

    token = result["access_token"]
    user = result.get("user")

    set_auth(url, token, user)
    name = user.get("name") or user.get("email") or user.get("username") if user else email
    click.echo(f"Logged in as {name}")
    click.echo(f"\nTrinity CLI is ready. Try 'trinity agents list'.")
