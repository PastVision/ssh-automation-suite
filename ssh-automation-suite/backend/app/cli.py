"""Account administration.

There is deliberately no HTTP route that creates users: an unauthenticated
"register" endpoint would be an open door, and an authenticated one would let any
account mint more accounts. Accounts are provisioned out of band with this CLI.

    python -m app.cli create-user alice --display-name "Alice Example"
    python -m app.cli set-password alice
    python -m app.cli list-users

The password is read from a prompt (never an argument, so it stays out of shell
history and process listings). ``--password-stdin`` exists for provisioning
scripts that already hold the secret.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.db.session import get_session_factory, reset_engine
from app.models.user import User
from app.services import sessions


def _read_password(confirm: bool = True) -> str:
    password = getpass.getpass("Password: ")
    if confirm:
        again = getpass.getpass("Repeat password: ")
        if password != again:
            raise SystemExit("Passwords did not match.")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    return password


def _read_password_stdin() -> str:
    password = sys.stdin.readline().rstrip("\n")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    return password


def create_user(args: argparse.Namespace) -> int:
    reset_engine()
    if sessions.get_user_by_username(args.username) is not None:
        print(f"User {args.username!r} already exists.", file=sys.stderr)
        return 1

    password = (
        _read_password_stdin() if args.password_stdin else _read_password()
    )
    user = sessions.create_user(
        args.username,
        password,
        display_name=args.display_name,
        is_active=not args.inactive,
    )
    print(f"Created user {user.username!r} (id {user.id}).")
    return 0


def set_password(args: argparse.Namespace) -> int:
    reset_engine()
    user = sessions.get_user_by_username(args.username)
    if user is None:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        return 1

    password = _read_password_stdin() if args.password_stdin else _read_password()
    sessions.set_password(user.id, password)
    # A password change invalidates every outstanding session, so a stolen cookie
    # cannot outlive the credential it was issued under.
    revoked = sessions.revoke_all_for_user(user.id, reason="password_changed")
    print(f"Password updated for {user.username!r}; {revoked} session(s) revoked.")
    return 0


def list_users(args: argparse.Namespace) -> int:
    reset_engine()
    session = get_session_factory()()
    try:
        rows = session.query(User).order_by(User.username).all()
        if not rows:
            print("No users. Create one with: python -m app.cli create-user <name>")
            return 0
        for row in rows:
            state = "active" if row.is_active else "disabled"
            print(f"{row.id:>4}  {row.username:<24} {state:<9} {row.display_name or ''}")
    finally:
        session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="SSH Automation Suite account administration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="Create an account")
    create.add_argument("username")
    create.add_argument("--display-name", default=None)
    create.add_argument(
        "--inactive", action="store_true", help="Create the account disabled"
    )
    create.add_argument("--password-stdin", action="store_true")
    create.set_defaults(func=create_user)

    pw = sub.add_parser("set-password", help="Change a password and revoke sessions")
    pw.add_argument("username")
    pw.add_argument("--password-stdin", action="store_true")
    pw.set_defaults(func=set_password)

    listing = sub.add_parser("list-users", help="List accounts")
    listing.set_defaults(func=list_users)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
