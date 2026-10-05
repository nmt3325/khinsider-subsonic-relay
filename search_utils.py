"""Small, deterministic search-normalization and ranking helpers."""
import unicodedata


def normalize_text(value):
    """NFKC/case-fold text and turn punctuation/symbol runs into spaces."""
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    out = []
    pending_space = False
    for ch in text:
        category = unicodedata.category(ch)
        if ch.isspace() or category[:1] in ('P', 'S'):
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(' ')
            pending_space = False
        out.append(ch)
    return ''.join(out).strip()


def compact_text(value):
    return normalize_text(value).replace(' ', '')


def terms(value):
    return normalize_text(value).split()


def _edit_distance_at_most_one(left, right):
    """Cheap Levenshtein<=1 test used only after an exact anchor matched."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        differences = 0
        for a, b in zip(left, right):
            if a != b:
                differences += 1
                if differences > 1:
                    return False
        return differences == 1
    i = j = differences = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        differences += 1
        if differences > 1:
            return False
        j += 1
    return True


def _short_ascii_term(term):
    return len(term) < 3 and term.isascii() and term.isalnum()


def rank_normalized(qnorm, tnorm, allow_fuzzy=True):
    """Rank already-normalized strings; returns None when they do not match."""
    if not qnorm or not tnorm:
        return None
    qcompact = qnorm.replace(' ', '')
    tcompact = tnorm.replace(' ', '')

    if qnorm == tnorm:
        return (0, len(tnorm))
    if qcompact == tcompact:
        return (1, len(tnorm))
    if tnorm.startswith(qnorm):
        return (2, len(tnorm))
    if qnorm in tnorm:
        return (3, len(tnorm))
    if len(qcompact) >= 3 and qcompact in tcompact:
        return (4, len(tnorm))

    qterms = qnorm.split()
    tterms = tnorm.split()
    positions = []
    missing = []
    exact = 0
    cursor = 0
    for term in qterms:
        if _short_ascii_term(term):
            try:
                token_index = tterms.index(term)
            except ValueError:
                missing.append(term)
                continue
            pos = tnorm.find(term, cursor)
            if pos < 0:
                pos = tnorm.find(term)
        else:
            pos = tnorm.find(term, cursor)
            if pos < 0:
                pos = tnorm.find(term)
            if pos < 0:
                missing.append(term)
                continue
        exact += 1
        positions.append(pos)
        cursor = max(cursor, pos + len(term))

    fuzzy = False
    if missing:
        if not (allow_fuzzy and len(missing) == 1 and exact >= 1):
            return None
        needle = missing[0]
        if not (needle.isascii() and needle.isalnum() and len(needle) >= 3):
            return None
        candidates = [
            token for token in tterms
            if token.isascii() and token.isalnum()
            and abs(len(token) - len(needle)) <= 1
        ]
        if not any(_edit_distance_at_most_one(needle, token) for token in candidates):
            return None
        fuzzy = True

    ordered = positions == sorted(positions)
    tier = 7 if fuzzy else (5 if ordered else 6)
    return (tier, len(tnorm))


def rank_text(query, target, allow_fuzzy=True):
    """Normalize and rank two strings."""
    return rank_normalized(
        normalize_text(query), normalize_text(target), allow_fuzzy=allow_fuzzy)


def matches_text(query, target, allow_fuzzy=True):
    return rank_text(query, target, allow_fuzzy=allow_fuzzy) is not None
