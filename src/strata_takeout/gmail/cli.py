"""strata-takeout-gmail: keep schema `gmail` of the shared database in step with a Gmail mbox from Google Takeout.

  sync [--force]                   every configured account; skips an mbox unchanged since the last load (and the same rules)
  ingest ACCOUNT TREE [--label L]  one mbox (or a folder holding one)
  doctor / set                     environment check / settings (strata-python)

Every message is loaded as metadata; the rules (rules.py) pick the ones that also keep their body text and attachments. Attachments are written
content-addressed under the attachment folder (setting `gmail_attachments`, env STRATA_TAKEOUT_GMAIL_ATTACHMENTS, or `--attachments DIR`); without
one no file is written (metadata and body text only).
Accounts are never defaulted: the job passes them, as setting `gmail_accounts` (env STRATA_TAKEOUT_GMAIL_ACCOUNTS), `id[:label]=tree[,...]`.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "gmail"
SETTINGS = Settings(
    "strata-takeout", [Setting("gmail_accounts", default=""), Setting("gmail_attachments", default="")]
)  # no defaults: which accounts, where the mbox is and where attachments go is the job's business


def accounts(spec: str) -> list[tuple[str, str | None, str]]:
    if not spec.strip():
        raise SystemExit(
            "no accounts: set STRATA_TAKEOUT_GMAIL_ACCOUNTS (id[:label]=tree,...) or use `ingest ACCOUNT TREE`"
        )
    out = []
    for part in spec.split(","):
        head, _, tree = part.strip().rpartition("=")
        if not head or not tree:
            raise ValueError(f"bad account {part!r}: want id[:label]=tree")
        aid, _, label = head.partition(":")
        out.append((aid, label or None, os.path.expanduser(tree)))
    return out


def _run(todo: list[tuple[str, str | None, str]], folder: str | None, force: bool) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session

    from .ingest import sync_mbox

    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-gmail.lock"), "a+"
    )
    with file_lock(lock):  # two syncs never interleave
        engine = connect(APP)
        Project.load(__file__, APP).upgrade(engine)
        for aid, label, tree in todo:
            with Session(engine) as s, s.begin():
                n = sync_mbox(s, aid, tree, os.path.expanduser(folder) if folder else None, label, force)
            print(f"{aid}: " + ("unchanged" if n is None else f"no mbox under {tree}" if n == 0 else f"{n} messages"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-gmail")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")
    s.add_argument("--attachments")
    s.set_defaults(
        fn=lambda a: _run(
            accounts(SETTINGS.get("gmail_accounts")),
            a.attachments or SETTINGS.get("gmail_attachments") or None,
            a.force,
        )
    )
    i = sub.add_parser("ingest", help="load one mbox")
    i.add_argument("account")
    i.add_argument("tree")
    i.add_argument("--label")
    i.add_argument("--attachments")
    i.add_argument("--force", action="store_true")
    i.set_defaults(
        fn=lambda a: _run(
            [(a.account, a.label, a.tree)], a.attachments or SETTINGS.get("gmail_attachments") or None, a.force
        )
    )
    cli.add_doctor(sub, "strata-takeout-gmail", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
