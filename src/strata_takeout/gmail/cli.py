"""strata-takeout-gmail: keep schema `gmail` of the shared database in step with a Gmail mbox from Google Takeout.

  sync [--force]                   every configured account; skips an mbox unchanged since the last load (and the same rules)
  ingest ACCOUNT TREE [--label L]  one mbox (or a folder holding one), or a gmvault backup folder (db/YYYY-MM.zip with .eml/.meta pairs)
  embed [--limit N]                embed the kept bodies that have no (current) vector (chunks table, pgvector); needs a strata-models service
  search QUERY [-k N]              the pieces of kept mail closest in meaning to QUERY
  doctor / set                     environment check / settings (strata-python)

Every message is loaded as metadata; the rules (rules.py) pick the ones that also keep their body text and attachments. Attachments are written
content-addressed under the attachment folder (setting `gmail_attachments`, env STRATA_TAKEOUT_GMAIL_ATTACHMENTS, or `--attachments DIR`); without
one no file is written (metadata and body text only).
Embedding runs after a load when `--embed` is given or setting `gmail_embed` (env STRATA_TAKEOUT_GMAIL_EMBED) is 1; it is skipped quietly when the
service is not running, and the next run catches up. Setting `gmail_verdicts` names the classifier verdicts file `sync` loads with.
Accounts whose mail has no junk worth sorting out (a work mailbox) can be kept whole: `--keep-all` on `ingest`, or setting `gmail_keep_all` (env STRATA_TAKEOUT_GMAIL_KEEP_ALL),
a comma-separated list of account ids, for `sync`: every message of those accounts keeps its body and attachments (reason `keep-all`), whatever the rules say.
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
    "strata-takeout",
    [
        Setting("gmail_accounts", default=""),
        Setting("gmail_attachments", default=""),
        Setting("gmail_verdicts", default=""),
        Setting("gmail_embed", default=""),
        Setting("gmail_keep_all", default=""),
    ],
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


def _run(
    todo: list[tuple[str, str | None, str]],
    folder: str | None,
    force: bool,
    verdicts: str | None = None,
    embed: bool = False,
    keep_all: frozenset[str] = frozenset(),
) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session

    from .ingest import sync_tree

    lock = open(
        os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "strata-takeout-gmail.lock"), "a+"
    )
    with file_lock(lock):  # two syncs never interleave
        engine = connect(APP)
        Project.load(__file__, APP).upgrade(engine)
        for aid, label, tree in todo:
            with Session(engine) as s, s.begin():
                n = sync_tree(s, aid, tree, os.path.expanduser(folder) if folder else None, label, force, verdicts)
            print(
                f"{aid}: "
                + (
                    "unchanged"
                    if n is None
                    else f"no mbox or gmvault backup under {tree}"
                    if n == 0
                    else f"{n} messages"
                )
            )
            if embed:
                _embed(engine, aid, None, quiet=True)
    return 0


def _embed(engine, account: str | None, limit: int | None, quiet: bool = False) -> int:
    """Embed what has no vector yet, committing every group. With `quiet` (after a load) a missing client or a service that is down is not an error."""
    from sqlalchemy.orm import Session

    from strata_takeout import embedding

    from .embed import counts, embed_pending

    if not embedding.available():
        print(
            "embedding skipped: strata-models-client is not installed (extra [gmail])"
            if quiet
            else "strata-models-client is not installed"
        )
        return 0 if quiet else 1
    import strata_models_client as sm

    with Session(engine) as s:
        try:
            res = embed_pending(s, account, limit, commit=s.commit)
        except (sm.Unavailable, sm.JobFailed) as e:
            s.rollback()
            print(f"embedding skipped: {e}" if quiet else f"embedding failed: {e}")
            return 0 if quiet else 1
        s.commit()
        print(
            f"embedded {res['messages']} messages, {res['chunks']} pieces; "
            + ", ".join(f"{k} {v}" for k, v in counts(s, account).items())
        )
    return 0


def _engine():
    from omarchy_db import Project, connect

    engine = connect(APP)
    Project.load(__file__, APP).upgrade(engine)
    return engine


def _search(a) -> int:
    from sqlalchemy.orm import Session

    from .embed import search

    with Session(_engine()) as s:
        for m, c, dist in search(s, a.query, a.k, account=a.account):
            body = (m.body_text or "")[c.start : c.end]
            print(
                f"{dist:.3f}  {m.at.date() if m.at else '?'}  {m.from_name or m.from_addr}: {m.subject}\n        {' '.join(body.split())[:160]}"
            )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-gmail")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")
    s.add_argument("--attachments")
    s.add_argument("--embed", action="store_true", help="embed the kept bodies afterwards (or set gmail_embed=1)")
    s.set_defaults(
        fn=lambda a: _run(
            accounts(SETTINGS.get("gmail_accounts")),
            a.attachments or SETTINGS.get("gmail_attachments") or None,
            a.force,
            SETTINGS.get("gmail_verdicts") or None,
            a.embed or SETTINGS.get("gmail_embed") == "1",
            frozenset(x.strip() for x in SETTINGS.get("gmail_keep_all").split(",") if x.strip()),
        )
    )
    i = sub.add_parser("ingest", help="load one mbox or gmvault backup")
    i.add_argument("account")
    i.add_argument("tree")
    i.add_argument("--label")
    i.add_argument("--attachments")
    i.add_argument("--verdicts", help="JSON-lines classifier verdicts: {offset, label, confidence, source}")
    i.add_argument("--force", action="store_true")
    i.add_argument("--embed", action="store_true", help="embed the kept bodies afterwards (or set gmail_embed=1)")
    i.add_argument(
        "--keep-all",
        action="store_true",
        help="keep every message of this account (body and attachments), whatever the rules say",
    )
    i.set_defaults(
        fn=lambda a: _run(
            [(a.account, a.label, a.tree)],
            a.attachments or SETTINGS.get("gmail_attachments") or None,
            a.force,
            a.verdicts,
            a.embed or SETTINGS.get("gmail_embed") == "1",
            frozenset({a.account}) if a.keep_all else frozenset(),
        )
    )
    e = sub.add_parser("embed", help="embed the kept bodies that have no (current) vector")
    e.add_argument("--account")
    e.add_argument("--limit", type=int)
    e.set_defaults(fn=lambda a: _embed(_engine(), a.account, a.limit))
    q = sub.add_parser("search", help="the pieces of kept mail closest in meaning to a query")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=8)
    q.add_argument("--account")
    q.set_defaults(fn=_search)
    cli.add_doctor(sub, "strata-takeout-gmail", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
