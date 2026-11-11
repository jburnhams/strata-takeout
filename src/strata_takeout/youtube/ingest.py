"""Load Takeout YouTube history files into the `youtube` schema, idempotently: a file whose (size, mtime) matches what `sources` recorded is skipped, a changed or new one replaces
every row of its (account, source) in one transaction, so a re-run (or a re-extract that rewrote the file) never duplicates or loses rows."""
import os
from datetime import datetime, timezone

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from .models import Account, Activity, Source
from .parse import parse_file

# source name -> path under the account's tree (same layout in every Google tree: Takeout/ is stripped by the inbox)
FILES = {'watch-history': 'YouTube and YouTube Music/history/watch-history.html', 'search-history': 'YouTube and YouTube Music/history/search-history.html', 'my-activity': 'My Activity/YouTube/My Activity.html'}
BATCH = 5000


def load_file(session: Session, account: str, source: str, path: str, st: os.stat_result) -> int:
    session.execute(delete(Activity).where(Activity.account == account, Activity.source == source))
    n, batch = 0, []
    for seq, e in enumerate(parse_file(path)):
        batch.append(dict(account=account, source=source, seq=seq, **e)); n += 1
        if len(batch) >= BATCH: session.execute(insert(Activity), batch); batch = []
    if batch: session.execute(insert(Activity), batch)
    row = session.get(Source, (account, source)); now = datetime.now(timezone.utc)
    if row is None: session.add(Source(account=account, source=source, path=path, size=st.st_size, mtime_ns=st.st_mtime_ns, rows=n, loaded_at=now))
    else: row.path, row.size, row.mtime_ns, row.rows, row.loaded_at = path, st.st_size, st.st_mtime_ns, n, now
    return n


def sync_tree(session: Session, account: str, tree: str, label: str | None = None, channel_id: str | None = None, force: bool = False) -> dict[str, int | None]:
    """{source: rows loaded, or None when unchanged}, for the files that exist under `tree`. The caller commits."""
    if not os.path.isdir(tree): return {}
    if session.get(Account, account) is None: session.add(Account(id=account, label=label, channel_id=channel_id)); session.flush()
    elif label or channel_id:
        a = session.get(Account, account); a.label = label or a.label; a.channel_id = channel_id or a.channel_id
    done: dict[str, int | None] = {}
    for source, rel in FILES.items():
        p = os.path.join(tree, rel)
        if not os.path.isfile(p): continue
        st = os.stat(p); prev = session.get(Source, (account, source))
        if not force and prev and (prev.size, prev.mtime_ns) == (st.st_size, st.st_mtime_ns): done[source] = None; continue
        done[source] = load_file(session, account, source, p, st)
    return done
