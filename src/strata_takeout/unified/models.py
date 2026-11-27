"""Schema `unified` holds only views (see `view.py`), so there are no tables to model; the app exists for its migrations."""

from omarchy_db import make_base

Base = make_base()
