"""Container entrypoint: hand /data to the service user, drop root, then start the server.

spooltag up to 0.3 ran as root, so an existing /data (and spooltag.db, which holds the API keys)
is owned by root. When the container starts as root this script, before anything else runs:
  1. makes every directory 0700 and every file 0600 (owner only) - symlinks are never followed,
  2. changes the owner of anything not yet owned by PUID:PGID (default 10001:10001),
  3. drops to PUID:PGID with no supplementary groups (all capabilities go with root),
then execs the command. Started as a non-root user already (compose `user:`), it only checks that
/data is writable and explains how to fix it if not. PUID=0 keeps the old run-as-root behaviour.
"""
import os
import stat
import sys

DEFAULT_ID = 10001


def log(msg: str) -> None:
    print(f"spooltag: {msg}", file=sys.stderr, flush=True)


def _ids() -> tuple[int, int]:
    """PUID/PGID (Unraid/linuxserver style). PGID defaults to PUID."""
    uid = int(os.environ.get("PUID", "").strip() or DEFAULT_ID)
    pgid = os.environ.get("PGID", "").strip()
    return uid, int(pgid) if pgid else uid


def prepare_data_dir(data: str, uid: int, gid: int) -> tuple[int, int]:
    """Run as root: owner-only modes and ownership for everything under `data`.
    Returns (items re-owned, items that couldn't be changed)."""
    os.makedirs(data, exist_ok=True)
    changed = failed = 0

    def fix(path: str, mode: int) -> None:
        nonlocal changed, failed
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                return
            if stat.S_IMODE(st.st_mode) != mode:
                os.chmod(path, mode)       # before chown: root still owns it, so no CAP_FOWNER needed
            if (st.st_uid, st.st_gid) != (uid, gid):
                os.lchown(path, uid, gid)
                changed += 1
        except OSError as e:
            failed += 1
            log(f"can't fix {path}: {e.strerror}")

    def walk_error(e: OSError) -> None:
        nonlocal failed
        failed += 1
        log(f"can't read {e.filename}: {e.strerror}")

    for root, dirs, files in os.walk(data, topdown=True, onerror=walk_error, followlinks=False):
        fix(root, 0o700)
        for name in files:
            fix(os.path.join(root, name), 0o600)
    return changed, failed


def writable(data: str) -> bool:
    db = os.path.join(data, "spooltag.db")
    if not os.access(data, os.W_OK | os.X_OK):
        return False
    return not os.path.exists(db) or os.access(db, os.R_OK | os.W_OK)


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise SystemExit("spooltag: refusing to run: privileges could not be dropped")


def main(argv: list[str]) -> int:
    if not argv:
        log("no command given (the image's CMD starts uvicorn)")
        return 64
    data = os.environ.get("DATA_DIR", "/data")
    uid, gid = _ids()
    if os.geteuid() == 0 and uid != 0:
        changed, failed = prepare_data_dir(data, uid, gid)
        if changed:
            log(f"{data}: gave {changed} item(s) to {uid}:{gid} (first start as a non-root user)")
        drop_privileges(uid, gid)
    elif os.geteuid() == 0:
        log("PUID=0: running as root (not recommended)")
    if not writable(data):
        log(f"{data} isn't writable by uid {os.geteuid()}. Either start the container as root once "
            f"(it then fixes ownership itself and drops to {uid}:{gid}), or on the Docker host run:\n"
            f"    sudo chown -R {os.geteuid()}:{os.getegid()} <your data folder> && "
            f"sudo chmod -R u+rwX,go-rwx <your data folder>")
        return 78
    os.umask(0o077)
    # The command comes from the image's CMD / compose `command:`, never from a request.
    os.execvp(argv[0], argv)  # nosec B606  # nosemgrep: python.lang.security.audit.dangerous-os-exec.dangerous-os-exec-audit
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
