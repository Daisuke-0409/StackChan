"""The order being talked about, before it becomes an order (再設計 §10).

Today a mobile order is parsed from a single sentence and built straight
into the site's cart. Real ordering does not sound like that. It sounds
like "ビッグマック" ... "あ、やっぱセット" ... "ランチなら L にして", and a
system that pushes each of those to a live cart ends up with three burgers
where a person meant one.

This module holds the thing that absorbs those turns instead: one draft,
edited in place, that only becomes a cart once it has stopped moving. It
is deliberately inert -- no network, no Playwright, no LLM, no menu. Every
name here is what the user said, not what the restaurant sells; resolving
`product` to a real `product_id` happens later, against a menu that was
actually fetched (§18). Keeping that separation is the point: the draft
can be reasoned about, printed and unit-tested without a browser.

The draft is not the source of truth for money. The readback and the
payment gate are still generated from the scraped cart (§21), because what
gets approved has to be what the site will charge for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace as replace_dataclass
from typing import Any, Optional

# A misheard number must not become 100 burgers. intent.detect() already
# refuses quantities outside this range for one-shot orders; the draft is
# the same promise for the conversational path.
MAX_QUANTITY = 10

# An item is `draft` while it is still just words, and `resolved` once it
# has been matched to something the store actually sells. Only resolved
# items can be built into a cart -- the check lives here so that "we never
# ordered a product nobody offers" is a property of the data, not of
# whichever code path happened to run.
STATUS_DRAFT = "draft"
STATUS_RESOLVED = "resolved"


class DraftError(ValueError):
    """The draft was asked for something that would make it incoherent."""


@dataclass
class Restaurant:
    """Where the order is going. `store_id` stays None until a store is picked."""

    chain: str
    store_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"chain": self.chain, "store_id": self.store_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Restaurant":
        return cls(chain=data["chain"], store_id=data.get("store_id"))


@dataclass
class DraftItem:
    """One line of the draft.

    `product` is the user's words ("ビッグマックセット"). `product_id` is the
    store's identifier for it, and stays None until resolution. `variant`
    carries the single/set distinction, which is a different axis from
    `size` -- "セットにして" and "L にして" are separate corrections and must
    not overwrite each other.

    `options` is free-form on purpose (drink, side, sauce, no-pickles).
    Which keys a chain understands is the adapter's business, and pinning
    that down here would put chain knowledge in the shared model (§9).
    """

    id: str
    product: str
    variant: Optional[str] = None
    size: Optional[str] = None
    quantity: int = 1
    options: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_DRAFT
    product_id: Optional[str] = None

    def __post_init__(self) -> None:
        self._check_quantity(self.quantity)

    @staticmethod
    def _check_quantity(quantity: int) -> None:
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise DraftError(f"数量が整数ではありません: {quantity!r}")
        if not 1 <= quantity <= MAX_QUANTITY:
            raise DraftError(f"数量 {quantity} は 1〜{MAX_QUANTITY} の範囲外です")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "product": self.product,
            "variant": self.variant,
            "size": self.size,
            "quantity": self.quantity,
            "options": dict(self.options),
            "status": self.status,
            "product_id": self.product_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftItem":
        return cls(
            id=data["id"],
            product=data["product"],
            variant=data.get("variant"),
            size=data.get("size"),
            quantity=data.get("quantity", 1),
            options=dict(data.get("options") or {}),
            status=data.get("status", STATUS_DRAFT),
            product_id=data.get("product_id"),
        )


@dataclass
class OrderDraft:
    """The whole draft: where, how it is collected, and what is in it.

    `active_item_id` is what "それ" refers to (§13). It is data rather than
    something inferred at use time because the referent is set by the
    conversation's history -- the last item added or touched -- and a later
    turn has no way to recover it.
    """

    restaurant: Restaurant
    fulfillment: Optional[str] = None
    items: list[DraftItem] = field(default_factory=list)
    active_item_id: Optional[str] = None
    _next_id: int = 1

    # --- lookup ------------------------------------------------------------

    def get(self, item_id: str) -> Optional[DraftItem]:
        return next((item for item in self.items if item.id == item_id), None)

    def require(self, item_id: str) -> DraftItem:
        item = self.get(item_id)
        if item is None:
            raise DraftError(f"item {item_id} は draft にありません")
        return item

    @property
    def active_item(self) -> Optional[DraftItem]:
        return self.get(self.active_item_id) if self.active_item_id else None

    def next_item_id(self) -> str:
        """Allocate an id. Ids are never reused, even after a removal.

        A removed item_2 coming back as a different product under the same
        name would make the audit trail lie about what was discussed.
        """
        item_id = f"item_{self._next_id}"
        self._next_id += 1
        return item_id

    # --- state -------------------------------------------------------------

    def is_resolved(self) -> bool:
        """True when every line has been matched to a real product."""
        return bool(self.items) and all(item.status == STATUS_RESOLVED
                                        for item in self.items)

    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    # --- operations (再設計 §11) -------------------------------------------
    #
    # The four mutating operations, and nothing else. The interpreter's job
    # ends at "the user meant REPLACE on item_1 with ダブチ"; what that does
    # to the draft is decided here, in plain Python, the same way every
    # time. An LLM that could edit the draft directly would be an LLM that
    # could order two burgers by being unlucky.

    def _target(self, item_id: Optional[str]) -> DraftItem:
        """The item an operation applies to. None means "the one we're on"."""
        if item_id is not None:
            return self.require(item_id)
        if self.active_item is None:
            raise DraftError("どの商品のことか分かりません")
        return self.active_item

    def _swap(self, old: DraftItem, new: DraftItem) -> DraftItem:
        self.items[self.items.index(old)] = new
        self.active_item_id = new.id
        return new

    def add(self, product: str, quantity: int = 1, **attributes: Any) -> DraftItem:
        """A further, separate product ("あとナゲットも").

        Lines are never merged, even for the same product. Two lines that
        look alike may have been meant as two, and folding them together
        would change a quantity nobody said out loud. If a merge is right,
        the interpreter asks for MODIFY instead.
        """
        item = DraftItem(id=self.next_item_id(), product=product,
                         quantity=quantity, **attributes)
        self.items.append(item)
        self.active_item_id = item.id
        return item

    def replace(self, product: str, item_id: Optional[str] = None,
                **attributes: Any) -> DraftItem:
        """This is a different product now ("ビッグマック……やっぱダブチ").

        The line keeps its id and its quantity; everything product-specific
        is cleared. Size, variant and options were chosen for the thing
        being replaced, and carrying "L" onto a product the user has said
        one word about would be inventing an order. Anything that really
        should survive is passed in explicitly by the interpreter, which
        heard the sentence.

        Resolution is dropped: a new product means a new product_id, and a
        stale one would build a cart containing the item that was retracted.
        """
        old = self._target(item_id)
        new = replace_dataclass(old, product=product, variant=None, size=None,
                                options={}, status=STATUS_DRAFT, product_id=None)
        if attributes:
            new = copy_item(new, **attributes)
        return self._swap(old, new)

    def modify(self, item_id: Optional[str] = None, **changes: Any) -> DraftItem:
        """Same product, different details ("それLにして", "コーラゼロで").

        Changing anything but the quantity un-resolves the line. A size or
        an option is part of what the store is being asked for, so the
        product_id found for the previous shape cannot be assumed to still
        apply -- STEP 7 resolves it again against the real menu.
        """
        if not changes:
            raise DraftError("変更内容が空です")
        if "product" in changes:
            raise DraftError("商品そのものの変更は replace を使ってください")
        old = self._target(item_id)
        new = copy_item(old, **changes)
        if set(changes) - {"quantity"}:
            new = copy_item(new, status=STATUS_DRAFT, product_id=None)
        return self._swap(old, new)

    def remove(self, item_id: Optional[str] = None) -> DraftItem:
        """Take a line out ("やっぱナゲットいらない").

        The conversation moves to whatever was most recently added, because
        that is what "それ" means next. With nothing left there is no
        referent, and the next "それ" has to be asked about.
        """
        item = self._target(item_id)
        self.items.remove(item)
        self.active_item_id = self.items[-1].id if self.items else None
        return item

    # --- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "restaurant": self.restaurant.to_dict(),
            "fulfillment": self.fulfillment,
            "items": [item.to_dict() for item in self.items],
            "active_item_id": self.active_item_id,
            "next_id": self._next_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderDraft":
        items = [DraftItem.from_dict(entry) for entry in data.get("items") or []]
        # Trust the stored counter, but never below the ids already present:
        # a truncated or hand-edited draft must not hand out an id twice.
        highest = 0
        for item in items:
            _, _, suffix = item.id.partition("_")
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return cls(
            restaurant=Restaurant.from_dict(data["restaurant"]),
            fulfillment=data.get("fulfillment"),
            items=items,
            active_item_id=data.get("active_item_id"),
            _next_id=max(int(data.get("next_id", 1)), highest + 1),
        )


def new_draft(chain: str, store_id: Optional[str] = None,
              fulfillment: Optional[str] = None) -> OrderDraft:
    """An empty draft for `chain`. Store and fulfillment may arrive later."""
    return OrderDraft(restaurant=Restaurant(chain=chain, store_id=store_id),
                      fulfillment=fulfillment)


def copy_item(item: DraftItem, **changes: Any) -> DraftItem:
    """A copy of `item` with `changes` applied, validated.

    Operations in STEP 3 edit through this rather than assigning fields, so
    an invalid quantity is refused at the point of change instead of
    surfacing at cart-build time when a person is waiting.
    """
    if "quantity" in changes:
        DraftItem._check_quantity(changes["quantity"])
    if "options" in changes:
        changes["options"] = dict(changes["options"] or {})
    return replace_dataclass(item, **changes)
