"""Transitive kinship inference.

Given the user-entered (manual) :class:`~backend.models.PersonRelation` rows,
derive every additional relation that logically follows — e.g. if A is the
father of B and B and C are brothers, then A is also the father of C (and C is
A's son).

A relation is stored as the directed triple ``(person, related, kind)`` meaning
"person is <kind> of related", matching the model's semantics. Inference works
on gender-neutral facts (parent / sibling / spouse / grandparent) and only emits
a preset relation when the subject's gender is known (inferred from the gendered
relation words the person already participates in).
"""
from typing import Iterable, Set, Tuple

Triple = Tuple[int, int, str]

MALE_KINDS = {"father", "son", "brother", "husband", "grandfather"}
FEMALE_KINDS = {"mother", "daughter", "sister", "wife", "grandmother"}

PARENT_KINDS = {"father", "mother"}
CHILD_KINDS = {"son", "daughter"}
SIBLING_KINDS = {"brother", "sister"}
SPOUSE_KINDS = {"husband", "wife"}
GRAND_KINDS = {"grandfather", "grandmother"}


def deduce(relations: Iterable[Triple]) -> Set[Triple]:
    """Return the closure of ``relations`` as a set of ``(person, related, kind)``
    triples. The returned set includes re-derivations of the inputs; the caller
    is expected to drop triples that already exist."""
    gender: dict = {}
    parents: Set[Tuple[int, int]] = set()  # (p, c): p is a parent of c
    siblings: Set[Tuple[int, int]] = set()  # symmetric
    spouses: Set[Tuple[int, int]] = set()  # symmetric
    grands: Set[Tuple[int, int]] = set()  # (g, c): g is a grandparent of c

    def note_gender(person: int, kind: str) -> None:
        if kind in MALE_KINDS:
            gender.setdefault(person, "M")
        elif kind in FEMALE_KINDS:
            gender.setdefault(person, "F")

    for a, b, kind in relations:
        if a is None or b is None or a == b:
            continue
        note_gender(a, kind)
        if kind in PARENT_KINDS:
            parents.add((a, b))
        elif kind in CHILD_KINDS:
            parents.add((b, a))
        elif kind in SIBLING_KINDS:
            siblings.add((a, b))
        elif kind in SPOUSE_KINDS:
            spouses.add((a, b))
        elif kind in GRAND_KINDS:
            grands.add((a, b))

    _close(parents, siblings, spouses, grands)

    out: Set[Triple] = set()
    gmap = {
        "parent": {"M": "father", "F": "mother"},
        "child": {"M": "son", "F": "daughter"},
        "sibling": {"M": "brother", "F": "sister"},
        "spouse": {"M": "husband", "F": "wife"},
        "grand": {"M": "grandfather", "F": "grandmother"},
    }

    def emit(person: int, related: int, role: str) -> None:
        kind = gmap[role].get(gender.get(person))
        if kind:
            out.add((person, related, kind))

    for p, c in parents:
        emit(p, c, "parent")
        emit(c, p, "child")
    for x, y in siblings:
        emit(x, y, "sibling")
    for x, y in spouses:
        emit(x, y, "spouse")
    for g, c in grands:
        emit(g, c, "grand")

    return out


def _close(parents, siblings, spouses, grands) -> None:
    """Expand the fact sets in place until no rule fires."""
    changed = True
    while changed:
        changed = False

        for x, y in list(siblings):
            if (y, x) not in siblings:
                siblings.add((y, x))
                changed = True
        for x, y in list(siblings):
            for y2, z in list(siblings):
                if y == y2 and x != z and (x, z) not in siblings:
                    siblings.add((x, z))
                    changed = True

        for x, y in list(spouses):
            if (y, x) not in spouses:
                spouses.add((y, x))
                changed = True

        # A parent of one sibling is a parent of all of them.
        for p, b in list(parents):
            for b2, c in list(siblings):
                if b == b2 and p != c and (p, c) not in parents:
                    parents.add((p, c))
                    changed = True

        # Children of the same parent are siblings.
        children: dict = {}
        for p, c in parents:
            children.setdefault(p, set()).add(c)
        for kids in children.values():
            kl = list(kids)
            for i in range(len(kl)):
                for j in range(len(kl)):
                    if i != j and (kl[i], kl[j]) not in siblings:
                        siblings.add((kl[i], kl[j]))
                        changed = True

        # A parent of a parent is a grandparent.
        for a, b in list(parents):
            for b2, c in list(parents):
                if b == b2 and a != c and (a, c) not in grands:
                    grands.add((a, c))
                    changed = True
