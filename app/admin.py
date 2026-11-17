"""Admin password recovery from a shell inside the container.

    docker exec spooltag python -m app.admin reset-password   # remove the password and log everyone out
    docker exec -it spooltag python -m app.admin set-password # set a new one (prompts)
"""
import getpass
import os
import sys

from .auth import HASH_KEY, MIN_LENGTH, hash_password
from .store import Store


def main(argv: list[str]) -> int:
    store = Store(os.environ.get("DATA_DIR", "/data"))
    cmd = argv[0] if argv else ""
    if cmd == "reset-password":
        store.delete_setting(HASH_KEY)
        store.delete_all_sessions()
        print("Admin password removed; Settings is open until a new one is set.")
        return 0
    if cmd == "set-password":
        pw = getpass.getpass("New admin password: ")
        if len(pw) < MIN_LENGTH or pw != getpass.getpass("Again: "):
            print(f"Passwords must match and be at least {MIN_LENGTH} characters.", file=sys.stderr)
            return 1
        store.save_settings({HASH_KEY: hash_password(pw)})
        store.delete_all_sessions()
        print("Admin password set; existing logins were signed out.")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
