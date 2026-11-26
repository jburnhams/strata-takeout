"""Which messages keep their body and attachments. Version `RULES` goes into `sources`, so a changed rule set reloads the file.

keep:  you sent it (label `sent`), or it is personal mail: not bulk, not an unsubscribe-able list, not from a no-reply/system sender, not spam/trash,
       and Gmail's own category is `personal` or absent (no category at all: old mail, or mail Gmail left uncategorised).
other: everything else, which keeps its metadata."""

import re

RULES = "r1"
NOREPLY = re.compile(r"no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster", re.I)
CATEGORY = "category "


def tier(labels: list[str], from_addr: str, has_unsubscribe: bool, is_bulk: bool) -> tuple[str, str]:
    """(tier, reason) of one message from its lower-case Gmail labels and headers."""
    ls = set(labels)
    if "sent" in ls:
        return "keep", "sent"
    if ls & {"spam", "trash"}:
        return "other", "spam-trash"
    if has_unsubscribe or is_bulk:
        return "other", "bulk"
    if NOREPLY.search(from_addr.split("@")[0]):
        return "other", "automated-sender"
    cats = {x for x in ls if x.startswith(CATEGORY)}
    if cats - {CATEGORY + "personal"}:
        return "other", "gmail-category"
    return "keep", "personal"
