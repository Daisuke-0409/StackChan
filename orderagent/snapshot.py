"""What exactly was approved, and whether it is still that (再設計 §22, §25).

An approval is not a mood. It is consent to one order, at one store, for
one number of yen, and it stops applying the moment any of those change.
This module is what makes that literal: a snapshot of the order as it was
read aloud, a fingerprint over its content, and a comparison that says --
in words a person can hear -- what moved.

    850円で承認 -> 決済直前に確認したら 900円
    -> 決済しない。「合計が850円から900円に変わったよ。もう一回確認して。」

The fingerprint covers identity and money only: the store, how it is
collected, the lines, the total. Not the timestamp, so the same order
approved twice fingerprints the same; not the store's display name, which
is cosmetic. Two snapshots agreeing means the user would say yes again.

created_at is kept outside the fingerprint and used for expiry instead. An
approval that has been sitting for five minutes is stale whether or not
anything changed -- the store, the queue and the person's mind all move.

payment_method_ref is a reference, never a number. The agent does not
handle payment details; Daisuke enters them into the browser profile by
hand, and this records only which one was meant.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import quote as quote_mod

# Matches the existing approval window, which is what the readback promises
# and what the expiry watcher already enforces.
DEFAULT_TTL_SECONDS = 300.0


@dataclass
class Snapshot:
    """The order as approved, frozen (§22)."""

    store_id: str
    fulfillment: Optional[str]
    lines: list[dict[str, Any]] = field(default_factory=list)
    total_yen: Optional[int] = None
    store_name: str = ""
    payment_method_ref: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    # --- identity ----------------------------------------------------------

    def content(self) -> dict[str, Any]:
        """The part an approval is actually about.

        Ordering of lines is preserved rather than sorted: the same items
        arriving in a different order is still the same order, but making
        that true would mean deciding what "same line" means, and every
        such decision is a way for a difference to be smoothed away. Two
        carts built from one draft come out in one order.
        """
        return {
            "store_id": self.store_id,
            "fulfillment": self.fulfillment,
            "total_yen": self.total_yen,
            "lines": [{"name": line["name"], "price": int(line["price"]),
                       "quantity": int(line.get("quantity", 1))}
                      for line in self.lines],
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.content(), sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def matches(self, other: "Snapshot") -> bool:
        return self.fingerprint() == other.fingerprint()

    # --- freshness ---------------------------------------------------------

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (time.time() if now is None else now) - self.created_at

    def is_expired(self, now: Optional[float] = None,
                   ttl: float = DEFAULT_TTL_SECONDS) -> bool:
        return self.age_seconds(now) > ttl

    # --- handing on --------------------------------------------------------

    def to_payment_expectation(self) -> dict[str, Any]:
        """The shape payment.verify_cart already takes.

        A bridge rather than a rewrite: the payment gate has been checking
        totals, counts and line items since before any of this existed, and
        it does not need to learn a new vocabulary to keep doing it.
        """
        return {
            "expected_total_yen": self.total_yen,
            "expected_item_count": sum(int(line.get("quantity", 1))
                                       for line in self.lines),
            "expected_items": [{"name": line["name"], "price": int(line["price"]),
                                "quantity": int(line.get("quantity", 1))}
                               for line in self.lines],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "store_name": self.store_name,
                "fulfillment": self.fulfillment,
                "lines": [dict(line) for line in self.lines],
                "total_yen": self.total_yen,
                "payment_method_ref": self.payment_method_ref,
                "created_at": self.created_at,
                "fingerprint": self.fingerprint()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(store_id=data["store_id"], store_name=data.get("store_name", ""),
                   fulfillment=data.get("fulfillment"),
                   lines=[dict(line) for line in data.get("lines") or []],
                   total_yen=data.get("total_yen"),
                   payment_method_ref=data.get("payment_method_ref"),
                   created_at=float(data.get("created_at", time.time())))


def from_resolved_order(order: quote_mod.ResolvedOrder,
                        payment_method_ref: Optional[str] = None,
                        now: Optional[float] = None) -> Snapshot:
    """Freeze what was just read aloud.

    Taking a ResolvedOrder rather than a cart is the point: a ResolvedOrder
    can only have come from the store's own cart (§21), so a snapshot
    cannot be made of an order nobody priced.
    """
    if order.total_yen is None:
        raise ValueError("金額が確定していない注文は承認できません")
    return Snapshot(
        store_id=order.store_id, store_name=order.store_name,
        fulfillment=order.fulfillment,
        lines=[line.to_dict() for line in order.lines],
        total_yen=order.total_yen,
        payment_method_ref=payment_method_ref,
        created_at=time.time() if now is None else now)


def changes(approved: Snapshot, current: Snapshot) -> list[str]:
    """What moved between approval and now, as sentences (§25).

    Returns empty when nothing did. Every entry is something the user
    consented to and no longer holds, so the caller's only correct response
    to a non-empty list is to stop and ask again.

    The total is reported first. It is the number a person remembers
    agreeing to, and burying it under a list of line changes makes the
    important part the hardest to hear.
    """
    problems: list[str] = []

    if approved.total_yen != current.total_yen:
        problems.append(f"合計が{approved.total_yen}円から{current.total_yen}円に変わったよ。")

    if approved.store_id != current.store_id:
        problems.append(f"店舗が{approved.store_name or approved.store_id}から"
                        f"{current.store_name or current.store_id}に変わったよ。")

    if approved.fulfillment != current.fulfillment:
        problems.append(
            f"受け取り方法が{quote_mod.spoken_fulfillment(approved.fulfillment)}から"
            f"{quote_mod.spoken_fulfillment(current.fulfillment)}に変わったよ。")

    problems.extend(quote_mod.reconcile(
        approved.lines, {"cart_items": current.lines,
                         "cart_total_yen": current.total_yen}))
    return problems


def verify(approved: Snapshot, cart: dict[str, Any],
           store_id: str, fulfillment: Optional[str],
           store_name: str = "", now: Optional[float] = None,
           ttl: float = DEFAULT_TTL_SECONDS) -> list[str]:
    """Re-check an approval against the store's cart, just before paying.

    Builds a fresh snapshot from the cart as it is right now and compares.
    Expiry is checked first: a stale approval is refused even when nothing
    changed, because consent given five minutes ago to something nobody has
    looked at since is not consent to spend money now.
    """
    if approved.is_expired(now, ttl):
        return ["承認から時間が経ちすぎているよ。もう一度確認して。"]

    current = Snapshot(
        store_id=store_id, store_name=store_name, fulfillment=fulfillment,
        lines=[line.to_dict() for line in quote_mod.lines_from_cart(cart)],
        total_yen=cart.get("cart_total_yen"))
    return changes(approved, current)
