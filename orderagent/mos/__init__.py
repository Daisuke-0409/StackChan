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
from .. import config, reconcile
from ..adapter import Capabilities

BASE = "https://netorder.mos.jp/pc"
STORE_SEARCH_URL = f"{BASE}/shop_search_top"
MENU_URL = f"{BASE}/menu_category"
SIGN_IN_URL = f"{BASE}/sign_in"

CLOSED_MARKER = "ネット注文は都合によりお休みしております"
ORDER_LINK_TEXT = "この店舗でお持ち帰り注文"
EARLIEST_PICKUP_TEXT = "最短でのお渡しで注文"
ADD_SINGLE_TEXT = "単品で追加"
CART_PAGE = "order_list"   # the site's own name; /cart is a 404


# Each cart row is its own list item, holding the product name, a quantity
# <select>, and the line price. Reading the flat text instead does not
# work: the quantity dropdown renders as the numbers 1 to 12, so the
# "number under a name" rule that parses the menu picks up "1" as a price.
_CART_JS = """
() => {
  const rows = [];
  // .card-item is the site's own row. Anchoring on the quantity <select>
  // and climbing instead matches six nested ancestors and reports one
  // burger as six -- the numbers 1..12 inside the dropdown also look
  // exactly like prices to a text-only reader.
  document.querySelectorAll('.card-item').forEach(card => {
    const select = card.querySelector('select');
    const lines = (card.innerText || '').split('\\n')
        .map(s => s.trim()).filter(Boolean);
    const name = lines[0] || '';
    if (!name) return;
    // The quantity dropdown contributes 1..12 to the text, so the price
    // is read from the element that is not inside the dropdown.
    const priceEl = [...card.querySelectorAll('*')].filter(e =>
        !e.closest('select') && !e.children.length &&
        /^[0-9,]{2,7}$/.test((e.innerText || '').trim()));
    const price = priceEl.length
        ? priceEl[priceEl.length - 1].innerText.trim() : null;
    if (!price) return;
    rows.push({name: name, price: price,
               quantity: select ? (select.value || '1') : '1'});
  });
  return rows;
}
"""


def _goto_settled(page: Any, url: str, attempts: int = 3) -> None:
    """goto that tolerates a navigation already under way."""
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as exc:  # noqa: BLE001
            if "interrupted by another navigation" not in str(exc):
                raise
            time.sleep(2)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)


def _clear_cart(page: Any, log: Callable[[str, dict], None]) -> None:
    """Empty the basket before building a new one.

    Every row carries its own 削除, so this is a loop rather than one
    button. Bounded, because a delete that does not stick would otherwise
    spin forever; the count is re-read each pass rather than trusted.
    """
    removed = 0
    for _ in range(20):
        # Reload each pass: confirming a delete navigates, which destroys
        # the execution context that a loop would otherwise keep using.
        # The delete's own navigation may still be in flight, and asking
        # for the same page mid-flight is an error rather than a no-op.
        _goto_settled(page, f"{BASE}/{CART_PAGE}")
        time.sleep(2.5)
        # 削除 opens a bootstrap modal, and the open modal covers the row
        # buttons -- a Playwright click waits for a landing the overlay
        # never allows, so both presses go through the DOM.
        target = page.evaluate(
            """() => {
              const b = [...document.querySelectorAll('.card-delete')]
                  .find(x => x.offsetParent);
              if (!b) return null;
              b.click();
              return b.getAttribute('data-target') || '';
            }""")
        if target is None:
            break
        time.sleep(1.5)
        try:
            page.evaluate(
                """(target) => {
                  const modal = target ? document.querySelector(target) : null;
                  const scope = modal || document;
                  const button = [...scope.querySelectorAll('button,a')]
                      .find(x => x.offsetParent &&
                            /削除|はい|OK/.test((x.innerText || '').trim()) &&
                            !x.classList.contains('card-delete'));
                  if (button) button.click();
                }""", target)
        except Exception:  # noqa: BLE001 -- the navigation IS the success
            pass
        time.sleep(2.5)
        removed += 1
    if removed:
        log("cart_cleared", {"rows_removed": removed})


def _scrape_cart(page: Any) -> dict[str, Any]:
    """What the cart page says: line items and the amount it will charge."""
    rows = page.evaluate(_CART_JS)
    cart_items = []
    for row in rows:
        name = row["name"].rstrip("※").strip()
        try:
            price = int(str(row["price"]).replace(",", ""))
            quantity = int(row["quantity"])
        except (TypeError, ValueError):
            continue
        cart_items.append({"name": name, "price": price, "quantity": quantity})

    total = None
    lines = [l.strip() for l in page.inner_text("body").split("\n") if l.strip()]
    for index, line in enumerate(lines):
        if "お支払い金額" in line:
            for candidate in lines[index:index + 3]:
                found = re.search(r"([0-9,]{2,7})\s*円", candidate)
                if found:
                    total = int(found.group(1).replace(",", ""))
                    break
        if total is not None:
            break
    return {"cart_items": cart_items, "cart_total_yen": total,
            "pickup_options": []}

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


# The site names its own card: .shopList-item holds exactly one store,
# name first, then address, distance, and either a wait or the closed
# notice. Climbing by content instead (the first ancestor that mentions a
# distance) lands one level short -- on the <dd> that omits the name --
# and the row then reports its own address as the shop's name.
_RESULTS_JS = """
() => {
  const out = [];
  document.querySelectorAll('.shopList-item').forEach((card, index) => {
    const lines = (card.innerText || '').split('\\n')
        .map(s => s.trim()).filter(Boolean);
    if (!lines.length) return;
    const text = card.innerText || '';
    out.push({
      name: lines[0],
      address: lines.find(l => l.includes('県')) || '',
      distance: lines.find(l => /^[0-9.]+\\s*(km|m)$/.test(l)) || '',
      closed: text.includes('ネット注文は都合によりお休みしております'),
      // 「20分後〜」 sits on the line after the label, and it is the part
      // worth saying out loud.
      wait: (() => {
        const at = lines.findIndex(l => l.includes('お渡し時間'));
        return at >= 0 && lines[at + 1] ? lines[at + 1] : '';
      })(),
      index: index,
    });
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


# The category pages, taken from the site's own nav rather than guessed.
MENU_PAGES = ("limited_menu", "tobikiri_menu", "burger_menu",
              "rice_burger_menu", "side_menu", "dessert_menu",
              "pack_menu", "kids_menu")

# Every product links to /menu/<code>, and that code is the handle worth
# keeping: it survives a renamed product and needs no text matching to
# click. The card's text carries the name and the price beneath it.
_MENU_JS = """
() => {
  const out = [];
  document.querySelectorAll('a[href*="menu/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const match = href.match(/menu\\/([A-Z0-9-]+)/);
    if (!match) return;
    const lines = (a.innerText || '').split('\\n')
        .map(s => s.trim()).filter(Boolean);
    if (lines.length < 2) return;
    const price = lines.find(l => /^[0-9,]{2,5}$/.test(l));
    if (!price) return;
    out.push({code: match[1], name: lines[0],
              price: parseInt(price.replace(',', ''), 10)});
  });
  return out;
}
"""


def _scrape_menu_page(page: Any) -> list[dict[str, Any]]:
    return [{"id": row["code"], "name": row["name"], "price": row["price"]}
            for row in page.evaluate(_MENU_JS)]


_NAV_WORDS = {
    "ログイン", "Language", "日本語", "English", "メニューTOP",
    "ネット注文特別価格メニュー", "限定メニュー", "とびきりバーガー 国産牛１００％",
    "ハンバーガー", "モスライスバーガー/ホットドッグ", "モスの菜摘", "ソイパティ",
    "サイドメニュー", "ドリンク/スープ", "デザート",
    "モスチキンパック・バラエティパック", "モスワイワイセット",
    "低アレルゲンメニュー", "朝モス",
}


class MosAdapter:
    chain = "mos"

    # Free-word search needs a word. Coordinates alone cannot be handed to
    # this site, so the prefecture is the coarse net and distance does the
    # actual ranking -- the browser's position is what it measures from.
    default_keyword = "宮崎"

    def find_stores(self, lat: float, lng: float,
                    limit: int = 3) -> list[dict[str, Any]]:
        with browser_mod.attached_page(latitude=lat, longitude=lng) as page:
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
        """Every orderable product, gathered across the category pages.

        Requires a store to have been entered first: the menu pages are
        rendered against the session's chosen branch, and asking for them
        cold gives the marketing catalogue instead of what this shop sells.
        """
        with browser_mod.attached_page() as page:
            items = []
            seen = set()
            for path in MENU_PAGES:
                _goto_settled(page, f"{BASE}/{path}")
                time.sleep(2.5)
                for row in _scrape_menu_page(page):
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    items.append(row)
            return items

    def promotions(self, store_id: str) -> set[str]:
        return set()

    def recent_orders(self, store_id: str) -> reconcile.Evidence:
        return reconcile.Evidence(
            history_available=False,
            note="モスの注文履歴の読み取りは未実装だよ。")

    def build_cart(self, store_id: str, items: list[dict[str, Any]],
                   job_id: str, log: Callable[[str, dict], None]) -> dict[str, Any]:
        """Add each item as a single product and read the cart back.

        「単品で追加」 rather than 「セットを選択」: a set is a different
        order at a different price, and nobody said the word.
        """
        job_dir = config.JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        with browser_mod.attached_page() as page:
            self._enter_store(page, store_id, log)
            # Unlike Starbucks, this site offers a 削除 on every row, so a
            # stale basket is a solvable problem rather than a refusal.
            _clear_cart(page, log)
            for item in items:
                self._add_item(page, str(item["id"]),
                               int(item.get("quantity", 1)), log)
            page.goto(f"{BASE}/{CART_PAGE}", wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)
            try:
                page.screenshot(path=str(job_dir / "cart.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            cart = _scrape_cart(page)
            cart["screenshots"] = str(job_dir)
            log("cart_scraped", cart)
            return cart

    def _enter_store(self, page: Any, store_id: str,
                     log: Callable[[str, dict], None]) -> None:
        """Search, pick the branch, accept the distance notice, take the
        earliest pickup slot. The session carries the store from here."""
        stores = search_stores(page, str(store_id))
        store = next((s for s in stores if s["id"] == str(store_id)), None)
        if store is None:
            raise RuntimeError(f"{store_id} が検索で見つからなかったよ")
        if not store["mop_enabled"]:
            raise RuntimeError(f"{store['name']} は今ネット注文をお休み中だよ")

        page.locator("a,button").filter(
            has_text=ORDER_LINK_TEXT).nth(store["index"]).click(timeout=10000)
        time.sleep(3)
        # 「選択した店舗から離れた場所にいるようです」 -- true and harmless;
        # the browser is at home and the shop is where it is.
        yes = page.get_by_text("はい", exact=True)
        if yes.count() and yes.first.is_visible():
            yes.first.click(timeout=8000)
            time.sleep(4)
        earliest = page.get_by_text(EARLIEST_PICKUP_TEXT, exact=True)
        if earliest.count():
            earliest.first.click(timeout=10000)
            time.sleep(4)
        log("store_entered", {"store": store["name"], "wait": store["wait"]})

    def _add_item(self, page: Any, product_code: str, quantity: int,
                  log: Callable[[str, dict], None]) -> None:
        """Open a product by its own code and add it as a single item.

        The code comes from menu(), so no text matching happens here: a
        product page is addressable, and hunting for a clickable ancestor
        of a name is how the wrong burger gets ordered.
        """
        _goto_settled(page, f"{BASE}/menu/{product_code}")
        time.sleep(2.5)
        for _ in range(max(0, quantity - 1)):
            plus = page.get_by_text("プラス", exact=True)
            if plus.count():
                plus.first.click(timeout=5000)
                time.sleep(1)
        add = page.get_by_text(ADD_SINGLE_TEXT, exact=True)
        if not add.count():
            raise RuntimeError(
                f"商品 {product_code} に「{ADD_SINGLE_TEXT}」が見つからないよ。"
                "今の時間は売っていないのかも。")
        add.first.click(timeout=10000)
        log("cart_add_clicked", {"code": product_code, "quantity": quantity})
        time.sleep(3)
