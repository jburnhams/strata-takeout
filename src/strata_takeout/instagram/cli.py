"""strata-takeout-instagram: keep schema `instagram` of the shared database in step with a Instagram export tree.

  sync [--force]            every configured account; skips a tree unchanged since the last load
  ingest ACCOUNT TREE       one extracted export tree (the folder holding `your_instagram_activity/`)
  doctor / set              environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `instagram_accounts` (env STRATA_TAKEOUT_INSTAGRAM_ACCOUNTS), `id[:label]=tree[,...]`.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "instagram"
SETTINGS = Settings(
    "strata-takeout", [Setting("instagram_accounts", default="")]
)  # which accounts and where the trees are is the job's business


def accounts(spec: str) -> list[tuple[str, str | None, str]]:
    if not spec.strip():
        raise SystemExit(
            "no accounts: set STRATA_TAKEOUT_INSTAGRAM_ACCOUNTS (id[:label]=tree,...) or use `ingest ACCOUNT TREE`"
        )
    out = []
    for part in spec.split(","):
        head, _, tree = part.strip().rpartition("=")
        if not head or not tree:
            raise ValueError(f"bad account {part!r}: want id[:label]=tree")
        aid, _, label = head.partition(":")
        out.append((aid, label or None, os.path.expanduser(tree)))
    return out


def _run(todo: list[tuple[str, str | None, str]], force: bool) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session

    from strata_takeout.facebook.ingest import sync_tree

    from . import models, parse

    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-instagram.lock"), "a+"
    )
    with file_lock(lock):  # two syncs never interleave
        engine = connect(APP)
        Project.load(__file__, APP).upgrade(engine)
        for aid, label, tree in todo:
            with Session(engine) as s, s.begin():
                n = sync_tree(s, aid, tree, label, force, models, parse)
            print(f"{aid}: " + ("unchanged" if n is None else f"no export under {tree}" if n == 0 else f"{n} rows"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-instagram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=lambda a: _run(accounts(SETTINGS.get("instagram_accounts")), a.force))
    i = sub.add_parser("ingest", help="load one export tree")
    i.add_argument("account")
    i.add_argument("tree")
    i.add_argument("--label")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=lambda a: _run([(a.account, a.label, a.tree)], a.force))
    cli.add_doctor(sub, "strata-takeout-instagram", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
