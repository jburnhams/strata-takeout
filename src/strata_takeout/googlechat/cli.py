"""strata-takeout-googlechat: keep schema `googlechat` of the shared database in step with Google chat exports.

  sync [--force]            every configured account; an input unchanged since the last load is skipped
  ingest ACCOUNT PATH...    one or more inputs: a Takeout tree (holding `Google Chat/Groups/`), a Gmail mbox (or a folder holding one), a gmvault backup tree
  doctor / set              environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `googlechat_accounts` (env STRATA_TAKEOUT_GOOGLECHAT_ACCOUNTS), `id[:label]=path[;path...][,...]`.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "googlechat"
SETTINGS = Settings(
    "strata-takeout", [Setting("googlechat_accounts", default="")]
)  # accounts and paths are the job's business


def accounts(spec: str) -> list[tuple[str, str | None, list[str]]]:
    if not spec.strip():
        raise SystemExit(
            "no accounts: set STRATA_TAKEOUT_GOOGLECHAT_ACCOUNTS (id[:label]=path[;path...],...) or use `ingest ACCOUNT PATH...`"
        )
    out = []
    for part in spec.split(","):
        head, _, paths = part.strip().partition("=")
        if not head or not paths:
            raise ValueError(f"bad account {part!r}: want id[:label]=path[;path...]")
        aid, _, label = head.partition(":")
        out.append((aid, label or None, [os.path.expanduser(p) for p in paths.split(";") if p]))
    return out


def _run(todo: list[tuple[str, str | None, list[str]]], force: bool) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session

    from . import ingest

    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-googlechat.lock"), "a+"
    )
    with file_lock(lock):  # two syncs never interleave
        engine = connect(APP)
        Project.load(__file__, APP).upgrade(engine)
        for aid, label, paths in todo:
            with Session(engine) as s, s.begin():
                res = ingest.sync(s, aid, paths, label, force)
            for source, n in res.items():
                print(f"{aid} {source}: " + ("unchanged" if n is None else f"{n} messages"))
            if not res:
                print(f"{aid}: no Google chat export under {', '.join(paths)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-googlechat")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=lambda a: _run(accounts(SETTINGS.get("googlechat_accounts")), a.force))
    i = sub.add_parser("ingest", help="load one or more inputs for an account")
    i.add_argument("account")
    i.add_argument("paths", nargs="+")
    i.add_argument("--label")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=lambda a: _run([(a.account, a.label, a.paths)], a.force))
    cli.add_doctor(sub, "strata-takeout-googlechat", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
