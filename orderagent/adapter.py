"""The seam between one order and any restaurant (再設計 §7-§9).

Everything a chain does differently -- its store API, its menu shape, its
DOM, its checkout -- lives behind this. The order engine above it knows
about products, sizes and quantities; it must never learn that McDonald's
serves its cart from a single-page app that forgets itself on navigation.
That fact is true today, was discovered the hard way, and is nobody's
business up here.

The interface is a Protocol rather than a base class on purpose: the
McDonald's adapter already exists as plain functions, and making it
conform should not mean rewriting it.

What lives here instead of in an adapter is the part that is the same
everywhere:

    resolve_conditions  "L if it's lunch" -> L, or M, once the store answers
    resolve_products    the user's words -> this store's product ids
    to_cart_items       a settled draft -> what build_cart takes

That order is not arbitrary. Answering a condition changes the size, and
changing the size un-resolves the line -- correctly, because "the L one" is
a different product from "the M one" and the store prices them apart. So
the questions get answered first, and only then is anything matched
against the menu. It is the order a person would use at the counter.

None of it touches a network. The store is reached only through whatever
adapter is passed in, so all of it can be tested against a fake one, and a
test can describe a store that has no drive-through, or a lunch promotion
that is not running, without waiting for a real Tuesday.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from . import draft as draft_mod

# §7. Not every chain, and not every branch of one chain, can do everything.
# A store that cannot take an online order can still be suggested, quoted
# and described, and that is worth having -- "there is a KFC nine minutes
# away but you will have to order at the counter" is a useful answer.
FULL_AUTO = "FULL_AUTO"        # order and pay online
ORDER_ONLY = "ORDER_ONLY"      # order online, pay in person
MENU_ONLY = "MENU_ONLY"        # can say what they sell and what it costs
UNSUPPORTED = "UNSUPPORTED"    # nothing we can do here


@dataclass
class Capabilities:
    """What this store can actually do, as opposed to what the chain can."""

    online_order: bool = False
    online_payment: bool = False
    drive_thru: bool = False
    pickup: bool = False
    delivery: bool = False
    promotions: bool = False
    coupons: bool = False
    menu: bool = True

    def level(self) -> str:
        if self.online_order and self.online_payment:
            return FULL_AUTO
        if self.online_order:
            return ORDER_ONLY
        if self.menu:
            return MENU_ONLY
        return UNSUPPORTED

    def supports_fulfillment(self, fulfillment: Optional[str]) -> bool:
        """Whether a requested collection method is available here.

        Unknown methods answer False rather than True: a fulfillment this
        code does not recognise is not one it can promise.
        """
        return {"drive_thru": self.drive_thru,
                "drive_through": self.drive_thru,
                "takeout": self.pickup,
                "pickup": self.pickup,
                "delivery": self.delivery}.get(fulfillment, False)

    def to_dict(self) -> dict[str, Any]:
        return {"online_order": self.online_order,
                "online_payment": self.online_payment,
                "drive_thru": self.drive_thru, "pickup": self.pickup,
                "delivery": self.delivery, "promotions": self.promotions,
                "coupons": self.coupons, "menu": self.menu,
                "level": self.level()}


@runtime_checkable
class RestaurantAdapter(Protocol):
    """One chain, seen from above.

    Menu entries are dicts with at least {id, name, price}, which is what
    the McDonald's side already produces and what build_cart already takes.
    Keeping that shape avoids a translation layer whose only job would be
    to rename fields.
    """

    chain: str

    def find_stores(self, lat: float, lng: float,
                    limit: int = 3) -> list[dict[str, Any]]:
        """Nearby stores, nearest first. [{id, name, distance_km, ...}]"""

    def capabilities(self, store_id: str) -> Capabilities:
        """What that store can do."""

    def menu(self, store_id: str) -> list[dict[str, Any]]:
        """This store's menu. Prices differ between branches of one chain."""

    def promotions(self, store_id: str) -> set[str]:
        """Which promotions are running here, right now.

        The names are the ones conditions ask about ("lunch", "coupon",
        "campaign"). An adapter that cannot tell returns an empty set, and
        the condition falls to its false branch -- which is the safe
        direction: it charges the ordinary price rather than promising a
        discount the store never offered.
        """

    def build_cart(self, store_id: str, items: list[dict[str, Any]],
                   job_id: str, log: Callable[[str, dict], None]) -> dict[str, Any]:
        """Put `items` in the store's cart and return what the SITE says.

        {"cart_items": [...], "cart_total_yen": int}. The reply is scraped,
        never assumed: the readback and the payment gate are both built
        from it, so what gets approved is what the site will charge for.
        """


# --- resolving names to products -------------------------------------------

@dataclass
class Resolution:
    """The outcome of matching a draft against a real menu."""

    resolved: list[str] = field(default_factory=list)       # item ids
    unmatched: list[str] = field(default_factory=list)      # spoken text
    interpretations: list[tuple[str, str]] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.unmatched


def resolve_products(order: draft_mod.OrderDraft, adapter: RestaurantAdapter,
                     matcher: Callable[[str, list[dict]], list[dict]],
                     guesser: Optional[Callable[[str, list[dict]], list[dict]]] = None
                     ) -> Resolution:
    """Turn what the user said into what this store sells.

    `matcher` is the cheap, deterministic pass; `guesser` is the expensive
    one that reads the menu with a model, for nicknames like ダブチ that are
    not even substrings of the real name. Both are passed in rather than
    imported so this stays testable without a network -- and so the order in
    which they are tried is visible here instead of buried.

    Every interpretation the guesser makes is recorded, because §18 requires
    the readback to say "I took ◯◯ to mean △△" out loud before anything is
    approved. A guess the user never hears is a guess they cannot correct.
    """
    result = Resolution()
    if not order.restaurant.store_id:
        raise draft_mod.DraftError("店舗が決まっていません")
    menu_items = adapter.menu(order.restaurant.store_id)

    for item in order.items:
        if item.status == draft_mod.STATUS_RESOLVED and item.product_id:
            result.resolved.append(item.id)
            continue
        matches = matcher(item.product, menu_items)
        if not matches and guesser is not None:
            matches = guesser(item.product, menu_items)
            if matches:
                result.interpretations.append((item.product, matches[0]["name"]))
        if not matches:
            result.unmatched.append(item.product)
            continue
        # The spoken name is left as the user said it. It is what the
        # readback and the audit trail quote back, and overwriting it with
        # the catalogue name would hide the interpretation being made.
        item.product_id = str(matches[0]["id"])
        item.status = draft_mod.STATUS_RESOLVED
        result.resolved.append(item.id)

    return result


# --- answering the conditions ----------------------------------------------

def resolve_conditions(order: draft_mod.OrderDraft,
                       adapter: RestaurantAdapter) -> list[tuple[str, bool]]:
    """Ask the store the questions the conversation left open (§17).

    This is the other half of STEP 6. The draft held "L if it is lunch"
    unanswered because nothing in it could see a clock or a campaign; the
    adapter can, so it is asked, and the branch it selects is applied.

    An adapter that cannot say returns nothing running, and the condition
    takes its false branch. That direction is deliberate: charging the
    ordinary price when a discount was in fact available is a smaller wrong
    than promising a discount that was not.

    Run this BEFORE resolve_products. Applying a branch changes the size,
    which un-resolves the line, so matching first only means matching
    twice.
    """
    if not order.restaurant.store_id:
        raise draft_mod.DraftError("店舗が決まっていません")
    pending = order.pending_conditions()
    if not pending:
        return []

    running = adapter.promotions(order.restaurant.store_id) or set()
    outcomes = []
    for condition in pending:
        if condition.kind != "promotion_available":
            # Unknown kinds stay pending rather than being answered by
            # default -- an unanswered question blocks the cart, which is
            # the right failure.
            continue
        met = condition.parameter in running
        order.resolve_condition(condition.id, met)
        outcomes.append((condition.id, met))
    return outcomes


# --- handing the draft to the cart -----------------------------------------

def to_cart_items(order: draft_mod.OrderDraft,
                  adapter: RestaurantAdapter) -> list[dict[str, Any]]:
    """A settled draft, in the shape build_cart already takes.

    Refuses anything unsettled. A draft with an unmatched product or an
    unanswered condition is not an order, and the failure has to happen
    here -- one step later it is a browser filling a real cart.
    """
    if not order.items:
        raise draft_mod.DraftError("注文が空です")
    unresolved = [i.product for i in order.items
                  if i.status != draft_mod.STATUS_RESOLVED or not i.product_id]
    if unresolved:
        raise draft_mod.DraftError(
            "メニューと突き合わせていない商品があります: " + "、".join(unresolved))
    if order.pending_conditions():
        raise draft_mod.DraftError("未解決の条件が残っています")

    menu_by_id = {str(entry["id"]): entry
                  for entry in adapter.menu(order.restaurant.store_id)}
    items = []
    for item in order.items:
        entry = menu_by_id.get(item.product_id)
        if entry is None:
            # The product resolved against a menu that no longer lists it.
            # Prices and availability move; discovering that now is far
            # better than discovering it mid-checkout.
            raise draft_mod.DraftError(
                f"{item.product} が現在のメニューに見つかりません")
        items.append({"id": entry["id"], "name": entry["name"],
                      "price": entry["price"], "quantity": item.quantity})
    return items
