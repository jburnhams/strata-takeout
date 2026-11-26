"""The cheap, obvious part of keep/drop. A message is kept when any keep signal holds (keep wins a conflict), dropped
when only junk signals hold, and left `undecided` otherwise for a classifier. Pure functions over the metadata `parse.headers`
returns, so they are tested without a mailbox. Nothing here names a person: the label sets are Gmail's own category
names and can be overridden by the job."""

from collections.abc import Iterable
from dataclasses import dataclass, field

KEEP_LABELS = frozenset(
    {"Sent", "Starred", "Category Personal", "Category Purchases", "Category Travel", "Category Bills"}
)
JUNK_LABELS = frozenset({"Spam", "Category Promotions", "Category Social", "Category Forums"})


@dataclass(frozen=True)
class Rules:
    keep_labels: frozenset[str] = KEEP_LABELS
    junk_labels: frozenset[str] = JUNK_LABELS
    replied_to: frozenset[str] = field(default_factory=frozenset)  # addresses the mailbox owner has written to


def replied_to(rows: Iterable[dict]) -> frozenset[str]:
    """Every address a sent message went to: mail FROM one of them is a conversation, not a broadcast."""
    out: set[str] = set()
    for r in rows:
        if r["is_sent"]:
            out.update(r["to_addrs"] or [])
    return frozenset(out)


def decide(row: dict, rules: Rules) -> tuple[str, str]:
    """(decision, rule name) for one metadata row."""
    labels = set(row["labels"] or [])
    if row["is_sent"]:
        return "keep", "sent"
    if labels & rules.keep_labels:
        return "keep", "keep-label:" + sorted(labels & rules.keep_labels)[0]
    if row["from_addr"] and row["from_addr"] in rules.replied_to:
        return "keep", "replied-to-sender"
    if row["has_unsubscribe"] or row["is_bulk"]:
        return "drop", "bulk-headers"
    if labels & rules.junk_labels:
        return "drop", "junk-label:" + sorted(labels & rules.junk_labels)[0]
    return "undecided", "rules"
