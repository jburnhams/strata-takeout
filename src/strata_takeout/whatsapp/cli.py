"""strata-takeout-whatsapp: keep schema `whatsapp` of the shared database in step with decrypted WhatsApp backups.

  sync [--force]                          every configured account; skips a backup unchanged since the last load
  ingest ACCOUNT TREE [--media DIR ...]   one backup, whether or not it is configured
  embed [--limit N]                       embed the conversation windows that have no (current) vector (needs a strata-models service)
  search QUERY [-k N]                     the conversation windows closest in meaning to QUERY
  doctor / set                            environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `whatsapp_accounts` (env STRATA_TAKEOUT_WHATSAPP_ACCOUNTS or
the settings file), `id[:label]=tree[,...]`, where `tree` holds (or is) the decrypted `msgstore.db` (with `wa.db` beside it),
and `whatsapp_media` (env STRATA_TAKEOUT_WHATSAPP_MEDIA), `id=dir[:dir...][,...]`, the folders the media files sit in.
Embedding runs after a load when `--embed` is given or setting `whatsapp_embed` (env STRATA_TAKEOUT_WHATSAPP_EMBED) is 1; it is skipped quietly when the
service is not running, and the next run catches up.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""

import argparse
import os
import sys
import tempfile

from omarchy_db import Project, connect
from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

APP = "whatsapp"
SETTINGS = Settings(
    "strata-takeout",
    [
        Setting("whatsapp_accounts", default=""),
        Setting("whatsapp_media", default=""),
        Setting("whatsapp_embed", default=""),
    ],
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


def _run(todo: list[tuple[str, str | None, str, list[str]]], force: bool, embed: bool = False) -> int:
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
            if embed:
                _embed(engine, aid, None, quiet=True)
    return 0


def _embed(engine, account: str | None, limit: int | None, quiet: bool = False) -> int:
    """Embed the windows with no vector yet, committing every group. With `quiet` (after a load) a missing client or a service that is down is not an error."""
    from sqlalchemy.orm import Session

    from strata_takeout import embedding

    from .windows import counts, embed_pending

    if not embedding.available():
        print(
            "embedding skipped: strata-models-client is not installed (extra [whatsapp])"
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
        print(f"embedded {res['windows']} windows; " + ", ".join(f"{k} {v}" for k, v in counts(s, account).items()))
    return 0


def _engine():
    engine = connect(APP)
    Project.load(__file__, APP).upgrade(engine)
    return engine


def _search(a) -> int:
    from sqlalchemy.orm import Session

    from .windows import search

    with Session(_engine()) as s:
        for w, text, dist in search(s, a.query, a.k, account=a.account):
            print(
                f"{dist:.3f}  {w.start_at:%Y-%m-%d %H:%M}  ({w.n_messages} messages)\n        {' '.join(text.split())[:200]}"
            )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-whatsapp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="load every configured account")
    s.add_argument("--force", action="store_true")
    s.add_argument(
        "--embed", action="store_true", help="embed the conversation windows afterwards (or set whatsapp_embed=1)"
    )

    def sync(a):
        dirs = media(SETTINGS.get("whatsapp_media"))
        return _run(
            [(i, lb, t, dirs.get(i, [])) for i, lb, t in accounts(SETTINGS.get("whatsapp_accounts"))],
            a.force,
            a.embed or SETTINGS.get("whatsapp_embed") == "1",
        )

    s.set_defaults(fn=sync)
    i = sub.add_parser("ingest", help="load one backup")
    i.add_argument("account")
    i.add_argument("tree")
    i.add_argument("--label")
    i.add_argument("--media", action="append", default=[], help="folder holding the media files (repeatable)")
    i.add_argument("--force", action="store_true")
    i.add_argument(
        "--embed", action="store_true", help="embed the conversation windows afterwards (or set whatsapp_embed=1)"
    )
    i.set_defaults(
        fn=lambda a: _run(
            [(a.account, a.label, a.tree, a.media)], a.force, a.embed or SETTINGS.get("whatsapp_embed") == "1"
        )
    )
    e = sub.add_parser("embed", help="embed the conversation windows that have no (current) vector")
    e.add_argument("--account")
    e.add_argument("--limit", type=int)
    e.set_defaults(fn=lambda a: _embed(_engine(), a.account, a.limit))
    q = sub.add_parser("search", help="the conversation windows closest in meaning to a query")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=8)
    q.add_argument("--account")
    q.set_defaults(fn=_search)
    cli.add_doctor(sub, "strata-takeout-whatsapp", "0.1.0", ("omarchy_db", "sqlalchemy"))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
