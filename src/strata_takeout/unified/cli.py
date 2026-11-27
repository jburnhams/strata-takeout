"""strata-takeout-unified: the cross-platform view `unified.messages_all` (Postgres only).

  build         migrate schema `unified` and (re)create the view over the platform schemas that exist now; run it again after a new platform is loaded
  doctor        environment check (strata-python)

The real database needs OMARCHY_DB_LIVE=1."""

import argparse
import sys

from strata_python import cli

APP = "unified"


def _build(_: argparse.Namespace) -> int:
    from omarchy_db import Project, connect

    from . import view

    engine = connect(APP)
    Project.load(__file__, APP).upgrade(engine)
    with engine.begin() as conn:
        have = view.build(conn)
    print("unified.messages_all over: " + (", ".join(have) or "(no platform loaded yet)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-takeout-unified")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="migrate and (re)create the view").set_defaults(fn=_build)
    cli.add_doctor(sub, "strata-takeout-unified", "0.1.0", ("omarchy_db", "sqlalchemy"))
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
