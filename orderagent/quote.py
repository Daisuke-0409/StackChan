"""What gets read aloud before anyone approves anything (再設計 §20-§21).

The rule this module exists to enforce: the readback is built from the
store's own cart, never from what we asked for. Those two can differ, and
the difference is exactly what a person needs to hear. On 2026-08-16 a
scrape keyed on the "変更" button silently lost the fries -- items that
cannot be customised have no such button -- and the order read back as
correct while being wrong. Reading back the request would have hidden it;
reading back the cart is what caught it.

So there is a build_from_cart() and deliberately no build_from_draft().
The draft is what the conversation settled on; the cart is what the store
will charge for. Only the second one can be approved.

reconcile() is the other half: it says where the two disagree, so a
missing line stops the order instead of being read past. Everything here
is a pure function over dictionaries, so all of it can be tested against
a cart that lost an item, priced one differently, or added one nobody
asked for -- none of which are easy to arrange on a real site on purpose.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# What the robot says for each way of collecting an order. Unknown methods
# are named as unknown rather than guessed at: a readback that invents a
# collection method is a readback nobody can check.
_FULFILLMENT_SPOKEN = {
    "drive_thru": "ドライブスルー受け取り",
    "drive_through": "ドライブスルー受け取り",
    "takeout": "お持ち帰り",
    "pickup": "お持ち帰り",
    "eatin": "店内",
    "delivery": "配達",
}

# Preference order when the store cannot do what was asked. Takeout first:
# it is the method every store offering online orders supports, and the one
# closest to driving up and collecting.
_FULFILLMENT_FALLBACKS = ("takeout", "pickup", "eatin")


@dataclass
class Line:
    """One line as the store's own cart shows it."""

    name: str
    price: int
    quantity: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "price": self.price, "quantity": self.quantity}


@dataclass
class ResolvedOrder:
    """A settled order, priced by the store, ready to be read aloud (§20)."""

    store_id: str
    store_name: str
    fulfillment: Optional[str]
    lines: list[Line] = field(default_factory=list)
    total_yen: Optional[int] = None
    interpretations: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "store_name": self.store_name,
                "fulfillment": self.fulfillment,
                "lines": [line.to_dict() for line in self.lines],
                "total_yen": self.total_yen,
                "interpretations": [list(pair) for pair in self.interpretations],
                "notes": list(self.notes)}


def _fold(name: str) -> str:
    """Names as compared, not as displayed: the site decorates with ® and spaces."""
    return re.sub(r"[\s()（）®]", "", unicodedata.normalize("NFKC", name)).lower()


def lines_from_cart(cart: dict[str, Any]) -> list[Line]:
    """The cart's rows, with identical ones added together for speech.

    A cart may hold the same product on several rows. Reading "ポテト 1点"
    three times is worse than saying it once with a count, and the count is
    what a person checks against what they meant to order.
    """
    counted: dict[tuple[str, int], int] = {}
    order: list[tuple[str, int]] = []
    for row in cart.get("cart_items") or []:
        key = (row["name"], row["price"])
        if key not in counted:
            order.append(key)
        counted[key] = counted.get(key, 0) + int(row.get("quantity", 1))
    return [Line(name=name, price=price, quantity=counted[(name, price)])
            for name, price in order]


def reconcile(requested: Iterable[dict[str, Any]],
              cart: dict[str, Any]) -> list[str]:
    """Where the store's cart differs from what was asked for.

    Returns spoken-ready sentences, empty when they agree. This is the
    check that catches a line the site dropped without saying so -- the
    failure that reads back as a correct, cheaper order.

    Prices are compared as the store states them. A price that moved
    between the menu and the cart is a real disagreement, not a rounding
    detail: it is the number the user is about to approve.
    """
    problems: list[str] = []

    wanted: dict[str, int] = {}
    wanted_price: dict[str, int] = {}
    display: dict[str, str] = {}
    for item in requested:
        key = _fold(item["name"])
        wanted[key] = wanted.get(key, 0) + int(item.get("quantity", 1))
        wanted_price[key] = int(item["price"])
        display[key] = item["name"]

    got: dict[str, int] = {}
    got_price: dict[str, int] = {}
    for line in lines_from_cart(cart):
        key = _fold(line.name)
        got[key] = got.get(key, 0) + line.quantity
        got_price[key] = line.price
        display.setdefault(key, line.name)

    for key, quantity in wanted.items():
        if key not in got:
            problems.append(f"{display[key]}がカートに入っていないよ。")
            continue
        if got[key] != quantity:
            problems.append(
                f"{display[key]}を{quantity}点頼んだのに、カートは{got[key]}点だよ。")
        if got_price[key] != wanted_price[key]:
            problems.append(
                f"{display[key]}の値段が{wanted_price[key]}円のはずが"
                f"{got_price[key]}円になっているよ。")

    for key, quantity in got.items():
        if key not in wanted:
            problems.append(f"頼んでいない{display[key]}がカートに入っているよ。")

    return problems


def choose_fulfillment(requested: Optional[str],
                       available: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    """The collection method to use, and what to say if it is not the one asked for.

    Stores differ within a chain -- 高鍋店 takes web orders but not at the
    drive-through. Substituting is fine; substituting silently is not, so
    the note is returned rather than swallowed and the caller is expected
    to read it out before asking for approval.
    """
    options = list(available or [])
    if requested and requested in options:
        return requested, None
    if not options:
        return requested, None
    for fallback in _FULFILLMENT_FALLBACKS:
        if fallback in options:
            if requested is None:
                return fallback, None
            return fallback, (f"この店は{spoken_fulfillment(requested)}が選べないから、"
                              f"{spoken_fulfillment(fallback)}にするね。")
    chosen = options[0]
    if requested is None:
        return chosen, None
    return chosen, (f"この店は{spoken_fulfillment(requested)}が選べないから、"
                    f"{spoken_fulfillment(chosen)}にするね。")


def spoken_fulfillment(fulfillment: Optional[str]) -> str:
    return _FULFILLMENT_SPOKEN.get(fulfillment, "受け取り方法未指定")


def build_from_cart(store_id: str, store_name: str, cart: dict[str, Any],
                    fulfillment: Optional[str] = None,
                    interpretations: Iterable[tuple[str, str]] = (),
                    notes: Iterable[str] = ()) -> ResolvedOrder:
    """A ResolvedOrder from what the store's cart actually holds (§20).

    There is no build_from_draft(). The draft is what the conversation
    settled on; this is what the store will charge for, and only the second
    one can be approved.
    """
    total = cart.get("cart_total_yen")
    return ResolvedOrder(
        store_id=store_id, store_name=store_name, fulfillment=fulfillment,
        lines=lines_from_cart(cart),
        total_yen=None if total is None else int(total),
        interpretations=[tuple(pair) for pair in interpretations],
        notes=list(notes))


def spoken(order: ResolvedOrder) -> str:
    """The readback (§21). Every number in it came from the store.

    Any interpretation made on the user's behalf is said first, before the
    order it affected -- a guess heard after the total is a guess that was
    not really offered for correction.
    """
    if order.total_yen is None:
        raise ValueError("金額が確定していない注文は読み上げられません")

    parts = ["".join(f"「{spoken_text}」は{resolved}のことだと解釈したよ。"
                     for spoken_text, resolved in order.interpretations)]
    parts.append("".join(order.notes))
    items = "、".join(f"{line.name} {line.quantity}点" for line in order.lines)
    parts.append(f"{order.store_name}、{items}、"
                 f"{spoken_fulfillment(order.fulfillment)}、"
                 f"合計{order.total_yen}円。")
    parts.append("注文は決済後キャンセルできないよ。注文していい？"
                 "「注文して」で確定、「キャンセル」で中止だよ。")
    return "".join(part for part in parts if part)
