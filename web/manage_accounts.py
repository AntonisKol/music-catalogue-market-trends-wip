"""Create and look after accounts. This is the only way an account can be made.

    python -m web.manage_accounts add <username>
    python -m web.manage_accounts set-password <username>
    python -m web.manage_accounts delete <username>
    python -m web.manage_accounts list

Passwords are typed at a hidden prompt, never given on the command line, where
they would stay in the shell's history. On the server, run it from the project folder:

    docker compose --env-file .env -f deploy/compose.yml run --rm api python -m web.manage_accounts add <username>
"""
import getpass
import sys
from datetime import datetime, timezone

import psycopg

from web import accounts, limits

USAGE = "Usage: python -m web.manage_accounts add|set-password|delete <username>, or list"


def ask_password() -> str:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Type it again: "):
        raise ValueError("The two passwords are not the same.")
    accounts.check_new_password(password)
    return password


def run(conn: psycopg.Connection, command: str, names: list) -> int:
    if command == "list":
        for username, created, last in accounts.list_accounts(conn):
            print(f"{username}  created {created:%Y-%m-%d}  last signed in {f'{last:%Y-%m-%d}' if last else 'never'}")
        return 0

    username = accounts.normalize_username(names[0])
    if command == "add":
        accounts.create_account(conn, username, ask_password(), datetime.now(timezone.utc))
        print(f"Account {username} created.")
        return 0
    if command == "set-password":
        if accounts.find_account(conn, username) is None:
            print(f"There is no account called {username}.", file=sys.stderr)
            return 1
        accounts.set_password(conn, username, ask_password())
        print(f"Password changed for {username}. Its sessions have ended, so it has to sign in again.")
        return 0

    account_id = accounts.find_account(conn, username)
    if account_id is None:
        print(f"There is no account called {username}.", file=sys.stderr)
        return 1
    accounts.delete_account(conn, account_id, f"account:{account_id}")
    print(f"Account {username} deleted.")
    return 0


def main(argv: list) -> int:
    known = (len(argv) == 1 and argv[0] == "list") or (len(argv) == 2 and argv[0] in ("add", "set-password", "delete"))
    if not known:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        with limits.connect_limits() as conn:
            return run(conn, argv[0], argv[1:])
    except accounts.AccountExists as taken:
        print(f"There is already an account called {taken}.", file=sys.stderr)
    except ValueError as problem:
        print(problem, file=sys.stderr)
    except psycopg.Error as problem:
        print(f"The database could not be used: {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
