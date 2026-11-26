"""strata-takeout-mail: keep schema `mail` of the shared database in step with mailbox exports.

  sync [--force]                      every configured account; skips an mbox unchanged since the last load
  ingest ACCOUNT MBOX [--attachments DIR]   one mbox, whether or not it is configured
  refill ACCOUNT                      bodies/attachments for rows since marked keep (by a classifier), no rescan
  doctor / set                        environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `mail_accounts` (env STRATA_TAKEOUT_MAIL_ACCOUNTS),
`id[:label]=mbox[,...]`, and `mail_attachments` (env STRATA_TAKEOUT_MAIL_ATTACHMENTS), `id=dir[,...]`, where attachment
files of kept mail are written. The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "mail"
SETTINGS = Settings("strata-takeout", [Setting("mail_accounts", default=""), Setting("mail_attachments", default="")])


def accounts(spec: str) -> list[tuple[str, str | None, str]]:
    if not spec.strip():
        raise SystemExit(
            "no accounts: set STRATA_TAKEOUT_MAIL_ACCOUNTS (id[:label]=mbox,...) or use `ingest ACCOUNT MBOX`"
        )
    out = []
    for part in spec.split(","):
        head, _, path = part.strip().rpartition("=")
        if not head or not path:
            raise ValueError(f"bad account {part!r}: want id[:label]=mbox")
        aid, _, label = head.partition(":")
        out.append((aid, label or None, os.path.expanduser(path)))
    return out


def attachments(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        aid, _, d = part.partition("=")
        if not aid or not d:
            raise ValueError(f"bad attachments entry {part!r}: want id=dir")
        out[aid] = os.path.expanduser(d)
    return out


def _engine():
    from omarchy_db import Project, connect

    engine = connect(APP)
    Project.load(__file__, APP).upgrade(engine)
    return engine


def _locked():
    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-mail.lock"), "a+"
    )
    return lock, file_lock(lock)


def _run(todo: list[tuple[str, str | None, str, str | None]], force: bool) -> int:
    from sqlalchemy.orm import Session

    from .ingest import sync_account

    lock, ctx = _locked()
    with ctx:  # two syncs never interleave
        engine = _engine()
        for aid, label, mbox, root in todo:
            with Session(engine) as s, s.begin():
                res = sync_account(s, aid, mbox, root, label, force)
            if res is None:
                print(f"{aid}: unchanged")
            elif not res:
                print(f"{aid}: no mbox at {mbox}")
            else:
                print(f"{aid}: " + ", ".join(f"{k} {v}" for k, v in res.items()))
    lock.close()
    return 0


def _refill(account: str, mbox: str, root: str | None) -> int:
    from sqlalchemy.orm import Session

    from .ingest import SOURCE, fill

    lock, ctx = _locked()
    with ctx:
        engine = _engine()
        with Session(engine) as s, s.begin():
            print(f"{account}: filled {fill(s, account, SOURCE, mbox, root)}")
    lock.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-mail")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")

    def sync(a):
        dirs = attachments(SETTINGS.get("mail_attachments"))
        return _run([(i, lb, p, dirs.get(i)) for i, lb, p in accounts(SETTINGS.get("mail_accounts"))], a.force)

    s.set_defaults(fn=sync)
    i = sub.add_parser("ingest", help="load one mbox")
    i.add_argument("account")
    i.add_argument("mbox")
    i.add_argument("--label")
    i.add_argument("--attachments", help="folder to write attachments of kept mail to (default: not written)")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=lambda a: _run([(a.account, a.label, a.mbox, a.attachments)], a.force))
    r = sub.add_parser("refill", help="bodies for rows marked keep since the load")
    r.add_argument("account")

    def refill(a):
        mbox = {i: p for i, _, p in accounts(SETTINGS.get("mail_accounts"))}[a.account]
        return _refill(a.account, mbox, attachments(SETTINGS.get("mail_attachments")).get(a.account))

    r.set_defaults(fn=refill)
    cli.add_doctor(sub, "strata-takeout-mail", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
