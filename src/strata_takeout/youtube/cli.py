"""strata-takeout-youtube: keep schema `youtube` of the shared database in step with the extracted Google trees.

  sync [--force]                              every configured account; skips files unchanged since the last load (run after each extract)
  ingest ACCOUNT TREE [--label L]   one tree, whether or not it is configured
  doctor / set                                 environment check / settings (strata-python)

Accounts are never defaulted: the job passes them, as setting `youtube_accounts` (env STRATA_TAKEOUT_YOUTUBE_ACCOUNTS or the settings file), `id=tree[,id=tree...]`; an id may carry a label as `id:label=tree`.
The real database needs OMARCHY_DB_LIVE=1, also set by the job."""
import argparse
import os
import sys

from strata_python import cli
from strata_python.oslib import file_lock
from strata_python.settings import Setting, Settings

SETTINGS = Settings('strata-takeout', [Setting('youtube_accounts', default='')])      # no default: which accounts and where their trees are is the job's business


def accounts(spec: str) -> list[tuple[str, str | None, str]]:
    out = []
    if not spec.strip(): raise SystemExit('no accounts: set STRATA_TAKEOUT_YOUTUBE_ACCOUNTS (id[:label]=tree,...) or use `ingest ACCOUNT TREE`')
    for part in spec.split(','):
        head, _, tree = part.strip().rpartition('=')
        if not head or not tree: raise ValueError(f'bad account {part!r}: want id[:label]=tree')
        aid, _, label = head.partition(':'); out.append((aid, label or None, os.path.expanduser(tree)))
    return out


def _run(todo, force: bool) -> int:
    from omarchy_db import Project, connect
    from sqlalchemy.orm import Session
    from .ingest import sync_tree
    lock = open(os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/tmp'), 'strata-takeout-youtube.lock'), 'a+')
    with file_lock(lock):                                       # two syncs (inbox and extract finishing together) never interleave
        engine = connect('youtube'); Project.load(__file__).upgrade(engine)
        for aid, label, tree in todo:
            with Session(engine) as s, s.begin():
                res = sync_tree(s, aid, tree, label, None, force)
            for source, n in res.items(): print(f'{aid} {source}: ' + ('unchanged' if n is None else f'{n} rows'))
            if not res: print(f'{aid}: nothing under {tree}')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='strata-takeout-youtube'); sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sync', help='load every configured account'); s.add_argument('--force', action='store_true'); s.set_defaults(fn=lambda a: _run(accounts(SETTINGS.get('youtube_accounts')), a.force))
    i = sub.add_parser('ingest', help='load one tree'); i.add_argument('account'); i.add_argument('tree'); i.add_argument('--label'); i.add_argument('--force', action='store_true')
    i.set_defaults(fn=lambda a: _run([(a.account, a.label, a.tree)], a.force))
    cli.add_doctor(sub, 'strata-takeout-youtube', '0.1.0', ('omarchy_db', 'sqlalchemy'))
    cli.add_set(sub, SETTINGS)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__': sys.exit(main())
