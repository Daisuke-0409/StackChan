"""Mos Burger, behind the RestaurantAdapter seam (再設計 §8, STEP 12 の2社目).

Scouted 2026-08-19 at night, when every branch had closed its order window,
so the parts that need a live cart are marked and left honest rather than
guessed. What was established:

- The site is server-rendered pages, not a single-page app. No pushState
  dance, no cart living in memory -- the McDonald's lessons do not apply.
- It moved domain on 2026-08-12: netorder.mos.co.jp -> netorder.mos.jp.
  The old host still redirects, but this uses the new one.
- Store search takes a free-word query and answers with distance from the
  browser's position, the current pickup wait, and whether net orders are
  open at all -- everything the readback needs, already computed.
- **Guest ordering is allowed.** The site says only that coupons need an
  account. That is the opposite of McDonald's, which closed guest ordering
  entirely, and it means this chain cannot lock us out the same way.
- Login, when wanted, is one plain form: mos.jp's own address and password.

Payment methods (公式FAQ): モスカード, credit card, Apple Pay, PayPay,
d払い. Which of those a logged-out order can reach is a live question for
the walkthrough -- entering a card number is not something this agent does,
so the usable ones are whatever the account already holds.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from .. import browser as browser_mod
from .. import reconcile
from ..adapter import Capabilities

BASE = "https://netorder.mos.jp/pc"
STORE_SEARCH_URL = f"{BASE}/shop_search_top"
MENU_URL = f"{BASE}/menu_category"
SIGN_IN_URL = f"{BASE}/sign_in"

CLOSED_MARKER = "ネット注文は都合によりお休みしております"
ORDER_LINK_TEXT = "この店舗でお持ち帰り注文"

_DISTANCE_RE = re.compile(r"^([0-9.]+)\s*(km|m)$")


class NotYetWalked(RuntimeError):
    """This part of the flow has not been seen with a live store.

    Raised rather than approximated: a cart built from guesswork is a
    payment made from guesswork, and every branch was closed the night
    this adapter was written.
    """


def _parse_distance_km(text: str) -> Optional[float]:
    match = _DISTANCE_RE.match((text or "").strip())
    if not match:
        return None
    value = float(match.group(1))
    return value if match.group(2) == "km" else value / 1000.0


def _set_position(page: Any, lat: float, lng: float) -> None:
    try:
        session = page.context.new_cdp_session(page)
        session.send("Emulation.setGeolocationOverride",
                     {"latitude": lat, "longitude": lng, "accuracy": 50})
    except Exception:  # noqa: BLE001
        pass


# Each result card carries the name, the address, the distance, and either
# a wait time or the closed notice. Anchored on the order link, which every
# card has exactly one of.
_RESULTS_JS = """
() => {
  const out = [];
  const links = [...document.querySelectorAll('a,button')]
      .filter(e => (e.innerText || '').includes('この店舗でお持ち帰り注文'));
  links.forEach((link, index) => {
    let card = link.parentElement;
    for (let k = 0; k < 8 && card; k++, card = card.parentElement) {
      const lines = (card.innerText || '').split('\\n')
          .map(s => s.trim()).filter(Boolean);
      const distance = lines.find(l => /^[0-9.]+\\s*(km|m)$/.test(l));
      const address = lines.find(l => l.includes('県') || l.includes('市'));
      if (distance && address && lines.length >= 3) {
        const text = card.innerText || '';
        out.push({
          name: lines[0],
          address: address,
          distance: distance,
          closed: text.includes('ネット注文は都合によりお休みしております'),
          wait: lines.find(l => l.includes('お渡し時間')) || '',
          index: index,
        });
        return;
      }
    }
  });
  return out;
}
"""


def search_stores(page: Any, keyword: str) -> list[dict[str, Any]]:
    """Stores matching a free-word query, nearest first.

    KNOWN DEFECT (2026-08-19): the card boundary is not right yet. A
    prefecture-wide search returns rows whose `name` holds the address of
    the next card down. It parses correctly for a narrow query
    ("宮崎大島"), so the fault is in how far _RESULTS_JS climbs, and it
    needs a live page with several results to fix. Until then this adapter
    is scouting equipment, not something to order through.
    """
    page.goto(STORE_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
    tab = page.get_by_text("フリーワード", exact=True)
    if tab.count():
        tab.first.click()
        time.sleep(1)
    box = page.locator("input[type=text]").first
    box.fill(keyword)
    # The search control is an anchor, not a button, and submitting from
    # the field is what a person does anyway.
    box.press("Enter")
    time.sleep(4)

    stores = []
    for row in page.evaluate(_RESULTS_JS):
        stores.append({
            "id": row["name"],
            "name": row["name"],
            "address": row["address"],
            "distance_km": _parse_distance_km(row["distance"]),
            "wait": row["wait"],
            "mop_enabled": not row["closed"],
            "index": row["index"],
        })
    stores.sort(key=lambda s: (s["distance_km"] is None, s["distance_km"] or 0))
    return stores


class MosAdapter:
    chain = "mos"

    # Free-word search needs a word. Coordinates alone cannot be handed to
    # this site, so the prefecture is the coarse net and distance does the
    # actual ranking -- the browser's position is what it measures from.
    default_keyword = "宮崎"

    def find_stores(self, lat: float, lng: float,
                    limit: int = 3) -> list[dict[str, Any]]:
        with browser_mod.attached_page() as page:
            _set_position(page, lat, lng)
            return search_stores(page, self.default_keyword)[:limit]

    def capabilities(self, store_id: str) -> Capabilities:
        with browser_mod.attached_page() as page:
            store = next((s for s in search_stores(page, str(store_id))
                          if s["id"] == str(store_id)), None)
        open_now = bool(store and store["mop_enabled"])
        return Capabilities(
            online_order=open_now,
            # 公式FAQ lists card/PayPay/d払い/モスカード for net orders, and
            # 2021 ended pay-at-the-counter for takeout. Whether a given
            # method is reachable depends on the account, and is settled in
            # the walkthrough.
            online_payment=open_now,
            drive_thru=False,
            pickup=open_now,
            delivery=False,      # a separate flow (delivery_search_top)
            promotions=False,
            coupons=False,       # members only, and not yet wired
            menu=True,
        )

    def menu(self, store_id: str) -> list[dict[str, Any]]:
        raise NotYetWalked(
            "モスのメニュー取得はまだ実地確認できてないよ（夜は全店休止中だった）。")

    def promotions(self, store_id: str) -> set[str]:
        return set()

    def recent_orders(self, store_id: str) -> reconcile.Evidence:
        return reconcile.Evidence(
            history_available=False,
            note="モスの注文履歴の読み取りは未実装だよ。")

    def build_cart(self, store_id: str, items: list[dict[str, Any]],
                   job_id: str, log: Callable[[str, dict], None]) -> dict[str, Any]:
        raise NotYetWalked(
            "モスのカート構築はまだ実装できてないよ。昼にお店が開いてる時間に作るね。")
