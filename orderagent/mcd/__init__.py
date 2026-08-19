"""McDonald's, seen through the RestaurantAdapter seam (再設計 STEP 7).

The thin wrapper the redesign notes asked for: every method delegates to
code that already exists and was already verified against the real site
(`stores.py`, `mcd_adapter.py`). Nothing here guesses a selector or an
API -- the two methods that would need new investigation (`promotions`,
`recent_orders`) return their documented safe emptiness instead, which the
engine above treats as "the store could not say" and falls to the careful
branch.

Per 競合A, `stores.py` is to be read as part of this adapter despite its
generic name; the common engine reaches store search only through here.
The rename waits for the second chain, when it becomes clear what is
actually common.
"""
from __future__ import annotations

from typing import Any, Callable

from .. import mcd_adapter, reconcile, stores
from ..adapter import Capabilities


class McdAdapter:
    chain = "mcd"

    def find_stores(self, lat: float, lng: float,
                    limit: int = 3) -> list[dict[str, Any]]:
        # The engine speaks of store `id`s; the POI list calls the same
        # value `key`. Both are kept so nothing downstream breaks either way.
        return [{**store, "id": store["key"]}
                for store in stores.nearest(lat, lng, limit=limit)]

    def capabilities(self, store_id: str) -> Capabilities:
        detail = stores.store_detail(store_id) or {}
        mop = bool(detail.get("mopEnabled"))
        return Capabilities(
            online_order=mop,
            # The web checkout takes card/PayPay when it works at all, so
            # payment capability follows order capability. Whether it works
            # TODAY is an operational fact (the 8/16 outage), not a
            # capability, and is discovered at cart time.
            online_payment=mop,
            # Store data does not say which branches offer drive-through
            # pickup on the web order (高鍋 does not, despite having a
            # physical drive-through). False is the promise-nothing answer;
            # the checkout page shows the truth and the readback announces
            # any fallback out loud.
            drive_thru=False,
            pickup=mop,
            delivery=bool(detail.get("mcDeliveryEnabled")),
            promotions=False,   # promotions() below cannot see them yet
            coupons=False,      # STEP 13
            menu=True,
        )

    def menu(self, store_id: str) -> list[dict[str, Any]]:
        return stores.menu(store_id)

    def promotions(self, store_id: str) -> set[str]:
        # Not yet investigated (the SPA carries ひるまック etc., but no
        # confirmed machine-readable source). Empty means every
        # promotion_available condition falls to its false branch: the
        # ordinary price, never a promised discount.
        return set()

    def build_cart(self, store_id: str, items: list[dict[str, Any]],
                   job_id: str, log: Callable[[str, dict], None]) -> dict[str, Any]:
        return mcd_adapter.build_cart(store_id, items, job_id, log)

    def recent_orders(self, store_id: str) -> reconcile.Evidence:
        # The web order shows 注文履歴 in its UI, so this is buildable --
        # but it has not been walked yet, and an unread history must
        # report itself as unread, not as empty (§27).
        return reconcile.Evidence(
            history_available=False,
            note="マックの注文履歴の読み取りは未実装だよ。注文番号の表示履歴を人が確認して。")
