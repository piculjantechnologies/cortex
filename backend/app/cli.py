"""Flask CLI commands: `flask --app run init-db` and `flask --app run make-admin <email>`."""

import click
from flask.cli import with_appcontext

from .extensions import db
from .models import User


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create the database tables that do not exist yet."""
    db.create_all()
    click.echo("Database tables are in place.")


@click.command("make-admin")
@click.argument("email")
@with_appcontext
def make_admin_command(email):
    """Give an existing user complimentary data access (the superuser flag)."""
    user = User.query.filter(db.func.lower(User.email) == email.strip().lower()).first()
    if user is None:
        raise click.ClickException(f"No user with email {email}")
    user.superuser = True
    db.session.commit()
    click.echo(f"{user.email} now has complimentary data access.")


def register_commands(app):
    app.cli.add_command(init_db_command)
    app.cli.add_command(make_admin_command)
