"""Finding out what happened when the answer never came (再設計 §26-§27).

A payment request went out and nothing came back. The store may be making
the food or may never have heard of it, and the two look identical from
here. §27 forbids the obvious move -- try again -- because the failure it
guards against is not "no lunch", it is "paid twice for one lunch".

So instead of retrying, this looks. It asks the adapter for the orders the
store thinks it has, and compares them to what was approved.

The verdict is three-valued, for the same reason the outcome was:

    CONFIRMED   one order matches. Here is its number.
    FAILED      the history was readable and holds nothing like it.
    UNRESOLVED  we could not tell. A person has to look.

UNRESOLVED is not a failure of this module; it is the honest answer to a
question the evidence does not settle, and it is the answer whenever the
history could not be read at all. Silence from a store is not proof that
nothing was ordered.

Two matches is the worst case and gets its own loud handling: it is the
only direct evidence of a double order, and the one situation where a
human needs to be told immediately rather than eventually.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import snapshot as snapshot_mod

CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
UNRESOLVED = "UNRESOLVED"

# How far around the attempt an order may sit and still be ours. Generous
# after, because a store's history can lag; short before, because an order
# placed a minute before the attempt was a different order.
WINDOW_BEFORE_SECONDS = 120.0
WINDOW_AFTER_SECONDS = 900.0


@dataclass
class Evidence:
    """What the store could be persuaded to say about its own orders.

    `history_available` is the field that matters most. An empty list
    because the store has no such order, and an empty list because the page
    would not load, mean opposite things, and collapsing them is how a
    successful order gets declared failed.
    """

    history_available: bool = False
    orders: list[dict[str, Any]] = field(default_factory=list)
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"history_available": self.history_available,
                "orders": [dict(o) for o in self.orders], "note": self.note}


@dataclass
class Verdict:
    """What we concluded, and what to say about it."""

    outcome: str
    order_number: Optional[str] = None
    matches: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def is_confirmed(self) -> bool:
        return self.outcome == CONFIRMED

    def needs_a_person(self) -> bool:
        return self.outcome == UNRESOLVED

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "order_number": self.order_number,
                "matches": [dict(m) for m in self.matches], "reason": self.reason}


def _looks_like(order: dict[str, Any], approved: snapshot_mod.Snapshot,
                attempted_at: float) -> bool:
    """Whether one history entry could be the order we tried to place.

    Store, total and timing. Deliberately not the line items: a store's
    history often abbreviates them, and demanding a match there would turn
    "cannot read the detail" into "not our order", which is the mistake
    this whole module exists to avoid.
    """
    if str(order.get("store_id")) != str(approved.store_id):
        return False
    if order.get("total_yen") != approved.total_yen:
        return False
    placed_at = order.get("placed_at")
    if placed_at is None:
        # A store that will not say when cannot rule the entry in or out on
        # time; store and total already agree, so keep it as a candidate
        # and let the caller see there was more than one if there was.
        return True
    return (attempted_at - WINDOW_BEFORE_SECONDS
            <= float(placed_at)
            <= attempted_at + WINDOW_AFTER_SECONDS)


def reconcile(approved: snapshot_mod.Snapshot, evidence: Evidence,
              attempted_at: float) -> Verdict:
    """Decide what happened, from what the store will admit to.

    Never speculates in the direction of "it was fine". Every path that is
    not clearly one order ends up in front of a person.
    """
    if not evidence.history_available:
        return Verdict(
            UNRESOLVED,
            reason=evidence.note or "注文履歴が読み取れなかったよ。")

    matches = [order for order in evidence.orders
               if _looks_like(order, approved, attempted_at)]

    if len(matches) == 1:
        order = matches[0]
        return Verdict(CONFIRMED, order_number=order.get("order_number"),
                       matches=matches,
                       reason="注文はお店に届いていたよ。")

    if not matches:
        return Verdict(FAILED, reason="お店の履歴に、この注文は入っていなかったよ。")

    return Verdict(UNRESOLVED, matches=matches,
                   reason=f"同じ内容の注文がお店の履歴に{len(matches)}件あるよ。"
                          "二重注文になっている可能性があるから、確認して。")


def spoken(verdict: Verdict) -> str:
    """What the robot says. Every branch ends with what happens next.

    An uncertain outcome is only useful to a person if it also says what
    was NOT done on their behalf, so the reassurance that no reorder was
    attempted is part of the sentence rather than a footnote.
    """
    if verdict.outcome == CONFIRMED:
        if verdict.order_number:
            return (f"{verdict.reason}注文番号は{verdict.order_number}だよ。"
                    f"お店で{verdict.order_number}って伝えてね。")
        return f"{verdict.reason}注文番号は分からなかったよ。"

    if verdict.outcome == FAILED:
        return f"{verdict.reason}注文は成立していないよ。もう一度頼むなら言ってね。"

    return (f"{verdict.reason}注文が成立したか確認できないから、"
            "二重注文にならないように再注文は止めているよ。お店に確認して。")
