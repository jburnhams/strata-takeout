"""Text embeddings for what the loaders keep: chunking, and asking a strata-models service (role `text-embed`) for the vectors.

Generic on purpose: no accounts, paths or model names live here. The service is found by `STRATA_MODELS_URL` (the client's own setting); which model it
serves is the service's registry decision, and the model that answered is recorded with every vector, so a changed model is noticed and redone.

  chunk(text)        (start, end) character offsets of the pieces of a body, split at paragraph breaks, each at most MAX_CHARS (a long paragraph is cut)
  prepare(text)      what is actually embedded for a piece: quoted lines dropped, links shortened (they say nothing and cost tokens)
  embed(texts)       (vectors, model) for a list of strings, in order, in batches through the service's bulk endpoint

`available()` says whether the client library is installed; the loaders skip embedding quietly when it is not or the service is down, and the next run
catches up (what has no vector yet is what gets embedded)."""

import re
from collections.abc import Sequence

APP = "strata-takeout"
ROLE = "text-embed"
MAX_CHARS = 1400
MIN_CHARS = (
    120  # a shorter body is one piece whatever its length; a trailing scrap shorter than this joins the piece before it
)
BATCH = 24
CHUNKER = "c1"  # bump when chunk() or prepare() change in a way that changes the vectors: it is part of what marks a row as embedded
URL = re.compile(r"https?://\S+")


def chunk(text: str, max_chars: int = MAX_CHARS) -> list[tuple[int, int]]:
    """Offsets of the pieces of `text`: blank-line separated paragraphs (one longer than `max_chars` is cut at a space) are packed together
    while they fit `max_chars` (a piece shorter than MIN_CHARS takes the next paragraph whatever its length); a trailing scrap shorter than MIN_CHARS joins the piece before it. Empty or whitespace-only text has no pieces."""
    units: list[tuple[int, int]] = []
    for m in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text):
        a, b = m.start(), m.end()
        while b - a > max_chars:
            cut = text.rfind(" ", a + max_chars // 2, a + max_chars)
            cut = cut if cut > a else a + max_chars
            units.append((a, cut))
            a = cut + 1 if text[cut : cut + 1] == " " else cut
        if b > a:
            units.append((a, b))
    spans: list[tuple[int, int]] = []
    for a, b in units:
        if spans and (
            b - spans[-1][0] <= max_chars or spans[-1][1] - spans[-1][0] < MIN_CHARS
        ):  # also: a tiny piece never stands alone
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < MIN_CHARS:
        spans[-2:] = [(spans[-2][0], spans[-1][1])]
    return spans


def prepare(piece: str) -> str:
    """The text a piece is embedded as: quoted reply lines dropped, links shortened, blank lines collapsed."""
    t = "\n".join(line for line in piece.splitlines() if not line.lstrip().startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", URL.sub("[link]", t)).strip()


def available() -> bool:
    try:
        import strata_models_client  # noqa: F401
    except ImportError:
        return False
    return True


def embed(texts: Sequence[str], batch: int = BATCH, client=None) -> tuple[list[list[float]], str]:
    """(vectors, model) of `texts`, in order. Raises the client's Unavailable when the service is not running and JobFailed when a job fails."""
    if client is None:
        import strata_models_client as client
    if not texts:
        return [], ""
    groups = [list(texts[i : i + batch]) for i in range(0, len(texts), batch)]
    out = client.run_many(ROLE, [dict(texts=g) for g in groups], APP, priority="low", batch=False)
    vectors: list[list[float]] = []
    model = ""
    for g, r in zip(groups, out, strict=True):
        if isinstance(r, Exception):
            raise r
        if len(r["vectors"]) != len(g):
            raise RuntimeError(f"the service returned {len(r['vectors'])} vectors for {len(g)} texts")
        vectors += r["vectors"]
        model = r.get("model") or model
    return vectors, model
