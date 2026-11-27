"""Which messages keep their body and attachments. Version `RULES` goes into `sources`, so a changed rule set reloads the file.

keep:  you sent it (label `sent`), or it is personal mail: not bulk, not an unsubscribe-able list, not from a no-reply/system sender, not spam/trash,
       and Gmail's own category is `personal`. Uncategorised mail (old mail, or mail Gmail left alone) is NOT decided here: on a real archive it is
       mostly shop and notification mail, so it stays `other` (`uncategorised`) until a later step classifies it.
other: everything else, which keeps its metadata, unless a classifier verdict (see `decide`) says it is worth keeping."""

import re

RULES = "r4"
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
    if not cats:
        return "other", "uncategorised"
    if cats - {CATEGORY + "personal"}:
        return "other", "gmail-category"
    return "keep", "personal"


KEEP_LABELS = frozenset({"personal", "transactional_keep"})


def decide(tier_: str, reason: str, verdict: dict | None) -> tuple[str, str]:
    """The rules' tier, overridden upwards by a classifier verdict: what the rules keep stays kept (sent and personal mail are never dropped by a
    model); an `other` message the classifier wants kept is kept, reason `classified`. The verdict says so with `keep` (true/false) when it
    has a decision of its own (a threshold, a second reader), else by its `label` being in KEEP_LABELS."""
    if tier_ == "other" and verdict:
        want = verdict["keep"] if "keep" in verdict else verdict.get("label") in KEEP_LABELS
        if want:
            return "keep", "classified"
    return tier_, reason
