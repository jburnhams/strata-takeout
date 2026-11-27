"""strata-takeout-whatsapp: keep schema `whatsapp` of the shared database in step with decrypted WhatsApp backups.

  sync [--force]                          every configured account; skips a backup unchanged since the last load
  ingest ACCOUNT TREE [--media DIR ...]   one backup, whether or not it is configured
  doctor / set                            environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `whatsapp_accounts` (env STRATA_TAKEOUT_WHATSAPP_ACCOUNTS or
the settings file), `id[:label]=tree[,...]`, where `tree` holds (or is) the decrypted `msgstore.db` (with `wa.db` beside it),
and `whatsapp_media` (env STRATA_TAKEOUT_WHATSAPP_MEDIA), `id=dir[:dir...][,...]`, the folders the media files sit in.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "whatsapp"
SETTINGS = Settings(
    "strata-takeout", [Setting("whatsapp_accounts", default=""), Setting("whatsapp_media", default="")]
)  # no defaults: which accounts and where their backups and media are is the job's business


def accounts(spec: str) -> list[tuple[str, str | None, str]]:
    if not spec.strip():
        raise SystemExit(
            "no accounts: set STRATA_TAKEOUT_WHATSAPP_ACCOUNTS (id[:label]=tree,...) or use `ingest ACCOUNT TREE`"
        )
    out = []
    for part in spec.split(","):
        head, _, tree = part.strip().rpartition("=")
        if not head or not tree:
            raise ValueError(f"bad account {part!r}: want id[:label]=tree")
        aid, _, label = head.partition(":")
        out.append((aid, label or None, os.path.expanduser(tree)))
    return out


def media(spec: str) -> dict[str, list[str]]:
    """`id=dir[:dir...],id2=...` -> {id: [dirs]}."""
    out: dict[str, list[str]] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        aid, _, dirs = part.partition("=")
        if not aid or not dirs:
            raise ValueError(f"bad media entry {part!r}: want id=dir[:dir...]")
        out.setdefault(aid, []).extend(os.path.expanduser(d) for d in dirs.split(os.pathsep) if d)
    return out


def _run(todo: list[tuple[str, str | None, str, list[str]]], force: bool) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session

    from .ingest import sync_account

    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-whatsapp.lock"), "a+"
    )
    with file_lock(lock):  # two syncs never interleave
        engine = connect(APP)
        Project.load(__file__, APP).upgrade(engine)
        for aid, label, tree, dirs in todo:
            with Session(engine) as s, s.begin():
                res = sync_account(s, aid, tree, dirs, label, force)
            if res is None:
                print(f"{aid}: unchanged")
            elif not res:
                print(f"{aid}: no msgstore.db under {tree}")
            else:
                print(f"{aid}: " + ", ".join(f"{k} {v}" for k, v in res.items()))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-whatsapp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")

    def sync(a):
        dirs = media(SETTINGS.get("whatsapp_media"))
        return _run([(i, lb, t, dirs.get(i, [])) for i, lb, t in accounts(SETTINGS.get("whatsapp_accounts"))], a.force)

    s.set_defaults(fn=sync)
    i = sub.add_parser("ingest", help="load one backup")
    i.add_argument("account")
    i.add_argument("tree")
    i.add_argument("--label")
    i.add_argument("--media", action="append", default=[], help="folder holding the media files (repeatable)")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=lambda a: _run([(a.account, a.label, a.tree, a.media)], a.force))
    cli.add_doctor(sub, "strata-takeout-whatsapp", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
