"""Starbucks, behind the RestaurantAdapter seam (再設計 §8, STEP 12).

The first chain that can actually take money. Its shape differs from
McDonald's in ways that matter to the order engine:

- Payment is a PREPAID card balance, not a credit card. The worst case of
  a runaway agent is bounded by what has been loaded onto the card, which
  is why this is the chain the payment code gets built against first.
- The site tells you the balance and says 「残高が不足しています」 before
  the pay button. That is a gift: insufficient funds is a fact this
  adapter can report, not an error discovered by clicking.
- Ordering requires a logged-in My Starbucks session. The session lives in
  the shared browser profile, put there by Daisuke's own hands; this code
  never sees a password. When it expires, the adapter says so and stops.

Everything runs through browser.attached_page -- an ordinary Chrome driven
over CDP -- because Playwright's own launch flags break this site's order
flow entirely (see browser.py).

The flow, as walked on 2026-08-19:

    /order/choose-store/list   pick a store        [選択する]
    /order/choose-usage        pick fulfillment    [選択する under TO GO]
    /order/choose-products     the menu
    /order/choose-products/<id>  size & quantity   [オーダーに追加]
    /order                     confirmation        [利用規約に同意の上、決済する]

The last screen is where this module stops. Payment belongs to payment.py,
behind the approval gate, and is not implemented here.
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable, Optional

from .. import browser as browser_mod
from .. import config, reconcile
from ..adapter import Capabilities

BASE = "https://webapp.starbucks.co.jp"
STORE_LIST_URL = f"{BASE}/order/choose-store/list"
PRODUCTS_URL = f"{BASE}/order/choose-products"
CONFIRM_URL = f"{BASE}/order"

_YEN_RE = re.compile(r"[¥￥]\s*([0-9,]+)")
_STORE_CACHE_SECONDS = 6 * 3600

# The site's own words for the states this adapter must recognise.
LOGIN_MARKERS = ("ログイン", "メールアドレス")
INSUFFICIENT_MARKER = "残高が不足しています"
PAY_BUTTON = "利用規約に同意の上、決済する"

FULFILLMENT_LABELS = {"drive_thru": "ドライブスルー",
                      "drive_through": "ドライブスルー",
                      "takeout": "TO GO",
                      "pickup": "TO GO",
                      "eatin": "店内飲食"}


class SessionExpired(RuntimeError):
    """The My Starbucks login is gone. A human must sign in again."""


class LeftoverCart(RuntimeError):
    """The account's basket already held something. A human must clear it.

    Starbucks keeps the cart against the logged-in account, and no way to
    empty it from this flow has been found (2026-08-19). Refusing here is
    better than reading back a total that is partly somebody else's
    afternoon.
    """


def _dismiss(page: Any) -> None:
    """Clear the transient 通信エラー dialog the app shows on cold start."""
    for _ in range(4):
        button = page.get_by_role("button", name="閉じる")
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=2500)
                time.sleep(0.6)
                continue
        except Exception:  # noqa: BLE001
            pass
        return


def _require_session(page: Any) -> None:
    if "login.starbucks.co.jp" in page.url:
        raise SessionExpired(
            "My Starbucks のログインが切れているよ。ブラウザで入り直してね。")


# Position handling lives in browser.py now: every attached page is
# granted geolocation for the ordering origins and given coordinates
# before it loads anything. An unset position is what produced the
# 「通信エラー」 that cost an evening (2026-08-19).


# The nearby list as the app renders it. Read from the DOM rather than
# from the API behind it: /resources/_execute-api multiplexes every
# operation onto one URL with an empty body, so a direct call cannot say
# which operation it wants -- and the rendered list carries more anyway.
# The site computes the distance and the wait itself, from the browser's
# own position, which is exactly the answer the readback should quote.
_NEARBY_JS = """
() => {
  const out = [];
  const selects = [...document.querySelectorAll('button')]
      .filter(b => b.innerText.trim() === '選択する');
  selects.forEach((b, index) => {
    // Climb until the ancestor holds the WHOLE card: a name line, an
    // address line and a distance. Stopping at the first element that
    // merely mentions a distance lands on the distance label itself.
    let card = b.parentElement;
    for (let k = 0; k < 8 && card; k++, card = card.parentElement) {
      const lines = (card.innerText || '').split('\\n')
          .map(s => s.trim()).filter(Boolean);
      const distanceLine = lines.find(l => /^[0-9.]+\\s*k?m$/.test(l));
      const hasAddress = lines.some(l => l.includes('県') || l.includes('市'));
      if (distanceLine && hasAddress && lines.length >= 3) {
        const t = card.innerText || '';
        out.push({
          name: lines[0],
          address: lines.find(l => l.includes('県') || l.includes('市')) || '',
          distance: distanceLine,
          wait: lines.find(l => l.includes('受取時間')) || '',
          closes: lines.find(l => l.includes('受付終了時間')) || '',
          orderable: !t.includes('受付時間外') && !b.disabled,
          index: index,
        });
        return;
      }
    }
  });
  return out;
}
"""


def _parse_distance_km(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        if text.endswith("km"):
            return float(text[:-2])
        if text.endswith("m"):
            return float(text[:-1]) / 1000.0
    except ValueError:
        return None
    return None


def nearby_stores(page: Any) -> list[dict[str, Any]]:
    """Stores near the browser's position, nearest first, as displayed."""
    page.goto(STORE_LIST_URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)
    _require_session(page)
    _dismiss(page)
    rows = page.evaluate(_NEARBY_JS)
    stores = []
    for row in rows:
        stores.append({
            "id": row["name"],          # the name IS the handle; see _open_store
            "name": row["name"],
            "address": row["address"],
            "distance_km": _parse_distance_km(row["distance"]),
            "wait": row["wait"],
            "closes": row["closes"],
            "mop_enabled": row["orderable"],
            "index": row["index"],
        })
    stores.sort(key=lambda s: (s["distance_km"] is None, s["distance_km"] or 0))
    return stores


class StarbucksAdapter:
    chain = "starbucks"

    def find_stores(self, lat: float, lng: float,
                    limit: int = 3) -> list[dict[str, Any]]:
        """Nearby stores as the site ranks them.

        lat/lng are accepted for the interface and used to move the
        browser's own position, because the site measures from there --
        asking it about somewhere else is not a thing it offers.
        """
        with browser_mod.attached_page(latitude=lat, longitude=lng) as page:
            return nearby_stores(page)[:limit]

    def capabilities(self, store_id: str) -> Capabilities:
        with browser_mod.attached_page() as page:
            store = next((s for s in nearby_stores(page)
                          if s["id"] == str(store_id)), None)
        mop = bool(store and store["mop_enabled"])
        return Capabilities(
            online_order=mop,
            online_payment=mop,   # prepaid card; balance is checked at confirm
            # Drive-through is offered per store on the usage screen, and the
            # store list does not say. Discovered at order time and announced
            # in the readback, never promised here.
            drive_thru=False,
            pickup=mop,
            delivery=False,
            promotions=False,
            coupons=False,
            menu=True,
        )

    def menu(self, store_id: str) -> list[dict[str, Any]]:
        """This store's menu, read off the product grid.

        Prices shown as a range (¥579〜¥668) are size-dependent; the low end
        is recorded, and the real price comes from the cart -- which is the
        number the readback and the payment check both use anyway.
        """
        with browser_mod.attached_page() as page:
            self._open_store(page, store_id)
            time.sleep(1.5)
            return self._scrape_menu(page)

    def promotions(self, store_id: str) -> set[str]:
        return set()

    def recent_orders(self, store_id: str) -> reconcile.Evidence:
        return reconcile.Evidence(
            history_available=False,
            note="スタバの注文履歴の読み取りは未実装だよ。アプリの注文履歴を人が確認して。")

    # --- internals ---------------------------------------------------------

    def _open_store(self, page: Any, store_id: str,
                    fulfillment: str = "takeout") -> None:
        """Walk store -> fulfillment -> menu, leaving the page on the menu."""
        stores = nearby_stores(page)
        store = next((s for s in stores if s["id"] == str(store_id)), None)
        if store is None:
            raise RuntimeError(f"{store_id} は近くの店舗リストに出ていないよ")
        if not store["mop_enabled"]:
            raise RuntimeError(f"{store['name']} は今オーダーの受付時間外だよ")

        # Click through Playwright, not JS: the app's handlers only respond
        # to real input events (a JS .click() selected the wrong card and
        # its dialog swallowed everything after).
        page.get_by_role("button", name="選択する").nth(store["index"]).click(timeout=10000)
        time.sleep(4)
        _dismiss(page)

        label = FULFILLMENT_LABELS.get(fulfillment, "TO GO")
        if "choose-usage" in page.url or "利用方法を選ぶ" in page.inner_text("body"):
            index = page.evaluate(
                """(label) => {
                  const btns=[...document.querySelectorAll('button')]
                    .filter(b => b.innerText.trim()==='選択する');
                  for(const b of btns){
                    let n=b;
                    for(let k=0;k<6&&n;k++,n=n.parentElement){
                      if((n.innerText||'').includes(label)) return btns.indexOf(b);
                    }
                  }
                  return -1;
                }""", label)
            if index < 0:
                raise RuntimeError(f"{label} での受け取りはこの店では選べないよ")
            page.get_by_role("button", name="選択する").nth(index).click(timeout=10000)
            time.sleep(4)
            _dismiss(page)

    def _scrape_menu(self, page: Any) -> list[dict[str, Any]]:
        # The category tabs sit in the same grid as the products and pick up
        # the price of whatever follows them. They are named by the page
        # itself, so ask it which names are categories rather than guessing.
        categories = set(page.evaluate(
            """() => [...document.querySelectorAll('button,a')]
                 .filter(e => e.offsetParent)
                 .map(e => e.innerText.trim())
                 .filter(t => t && t.length < 25)"""))
        rows = page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              document.querySelectorAll('*').forEach(el => {
                if (el.children.length) return;
                const t = (el.innerText || '').trim();
                if (!t || t.length > 40) return;
                let parent = el.parentElement;
                for (let k = 0; k < 3 && parent; k++, parent = parent.parentElement) {
                  const pt = parent.innerText || '';
                  const m = pt.match(/[\\u00A5\\uFFE5]\\s*([0-9,]+)/);
                  if (m && pt.trim().startsWith(t)) {
                    const key = t + '|' + m[1];
                    if (!seen.has(key)) { seen.add(key); out.push({name: t, price: m[1]}); }
                    return;
                  }
                }
              });
              return out;
            }""")
        menu = []
        seen = set()
        for row in rows:
            name = row["name"].strip()
            if not name or name.startswith("¥") or name in categories:
                continue
            # Notes and disclaimers share the grid with the products and
            # inherit the next price; nothing orderable talks like this.
            if any(mark in name for mark in ("価格", "税込", "※", "含みます")):
                continue
            if name in seen:
                continue
            try:
                price = int(row["price"].replace(",", ""))
            except ValueError:
                continue
            seen.add(name)
            # The product id is discovered when the item is opened; the name
            # is the handle until then, which is what the matcher works on.
            menu.append({"id": name, "name": name, "price": price})
        return menu

    def build_cart(self, store_id: str, items: list[dict[str, Any]],
                   job_id: str, log: Callable[[str, dict], None]) -> dict[str, Any]:
        """Put the items in the cart and read back the confirmation screen.

        Returns the same shape the McDonald's adapter does, plus what only
        this chain can say: the card balance and whether it covers the bill.
        """
        job_dir = config.JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        fulfillment = (items[0].get("fulfillment") if items else None) or "takeout"

        with browser_mod.attached_page() as page:
            self._open_store(page, store_id, fulfillment)
            log("store_opened", {"store_id": store_id, "fulfillment": fulfillment})

            for item in items:
                for _ in range(int(item.get("quantity", 1))):
                    self._add_item(page, item["name"], log)

            confirm = page.get_by_role("button", name="注文内容を確認")
            if not confirm.count():
                raise RuntimeError("「注文内容を確認」が見つからないよ")
            confirm.first.click(timeout=10000)
            time.sleep(5)
            _dismiss(page)
            try:
                page.screenshot(path=str(job_dir / "confirm.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            cart = self._scrape_confirmation(page)
            cart["screenshots"] = str(job_dir)
            log("cart_scraped", cart)

            # A leftover cart is the failure this chain is prone to: the
            # basket belongs to the logged-in account and survives
            # everything, and this site offers no way to empty it that has
            # been found. Say so plainly rather than letting the readback
            # quote a total that includes yesterday's frappuccino. The
            # payment gate would refuse it anyway; this turns a confusing
            # refusal into an instruction.
            wanted = {}
            for item in items:
                wanted[item["name"]] = wanted.get(item["name"], 0) + int(item.get("quantity", 1))
            found: dict[str, int] = {}
            for line in cart["cart_items"]:
                found[line["name"]] = found.get(line["name"], 0) + line["quantity"]
            strays = []
            for name, count in found.items():
                expected = next((c for n, c in wanted.items()
                                 if n in name or name in n), 0)
                if count > expected:
                    strays.append(f"{name} {count - expected}点")
            if strays:
                raise LeftoverCart(
                    "カートに前の注文が残ってるよ（" + "、".join(strays) +
                    "）。ブラウザで空にしてから、もう一度言ってね。")
            return cart

    def _add_item(self, page: Any, name: str,
                  log: Callable[[str, dict], None]) -> None:
        opened = page.evaluate(
            """(name) => {
              const els=[...document.querySelectorAll('*')]
                .filter(e => !e.children.length && (e.innerText||'').trim() === name);
              if (!els.length) return false;
              let n = els[0];
              for (let k=0; k<5 && n; k++, n=n.parentElement) {
                if (n.tagName==='BUTTON' || n.tagName==='A' ||
                    n.getAttribute('role')==='button' || n.onclick) { n.click(); return true; }
              }
              els[0].click();
              return true;
            }""", name)
        if not opened:
            raise RuntimeError(f"「{name}」がこの店のメニューに見つからないよ")
        time.sleep(3)
        add = page.get_by_role("button", name="オーダーに追加")
        if not add.count():
            raise RuntimeError(f"「{name}」をオーダーに追加できなかったよ")
        add.first.click(timeout=10000)
        log("cart_add_clicked", {"item": name})
        time.sleep(3)

    def _scrape_confirmation_static(self, text: str) -> dict[str, Any]:
        """_scrape_confirmation over text that is already in hand."""
        class _Held:
            def inner_text(self, _selector):
                return text
        return self._scrape_confirmation(_Held())

    def _scrape_confirmation(self, page: Any) -> dict[str, Any]:
        """What 注文内容を確認する says: lines, total, balance, shortfall."""
        text = page.inner_text("body")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        cart_items = []
        for index, line in enumerate(lines):
            match = re.match(r"^(.+?)\s*([0-9]+)点$", line)
            if not match:
                continue
            price = None
            for following in lines[index + 1:index + 3]:
                amount = _YEN_RE.search(following)
                if amount:
                    price = int(amount.group(1).replace(",", ""))
                    break
            if price is not None:
                cart_items.append({"name": match.group(1).strip(),
                                   "price": price,
                                   "quantity": int(match.group(2))})

        def _labelled(pattern: str) -> Optional[int]:
            for index, line in enumerate(lines):
                if re.search(pattern, line):
                    for candidate in lines[index:index + 2]:
                        amount = _YEN_RE.search(candidate)
                        if amount:
                            return int(amount.group(1).replace(",", ""))
            return None

        total = _labelled(r"総合計")
        balance = _labelled(r"残高")
        return {
            "cart_items": cart_items,
            "cart_total_yen": total,
            "balance_yen": balance,
            "sufficient_balance": (INSUFFICIENT_MARKER not in text),
            "pickup_options": [],
        }


# --- payment (called only from payment.execute, behind every guard) --------

_ORDER_NUMBER_RE = re.compile(r"(?:注文番号|受付番号|オーダー番号)[^0-9A-Za-z]*([0-9A-Za-z\-]{3,20})")


def confirm_payment(job: dict[str, Any], cart: dict[str, Any]) -> dict[str, Any]:
    """Press 「利用規約に同意の上、決済する」. Once. Never twice.

    Called only from payment.execute(), which has already checked that the
    approved order matches this cart, that the total is under the cap, and
    that the card covers it. What is left here is the click and the reading
    of whatever comes back.

    The one thing this function must never do is retry. If the outcome
    cannot be read -- a timeout, a page that goes somewhere unfamiliar --
    it returns payment_executed with outcome UNKNOWN, and a person checks
    the order history. Pressing again to find out would be how one coffee
    becomes two.
    """
    adapter = StarbucksAdapter()
    with browser_mod.attached_page() as page:
        # The browser must still be looking at the confirmation screen that
        # was scraped and approved. Anything else means the state moved
        # underneath us, and this is the last moment it is free to stop.
        if not page.url.rstrip("/").endswith("/order"):
            raise RuntimeError("決済画面から離れてしまったよ。もう一度最初からお願い。")
        before = page.inner_text("body")
        if PAY_BUTTON not in before:
            raise RuntimeError("決済ボタンが見つからないよ。画面が変わったのかも。")

        # Re-read the screen one last time, right where the click happens.
        # The cart passed in was read minutes ago, on the other side of a
        # conversation; if the store changed a price since, this catches it.
        final = adapter._scrape_confirmation_static(before)
        if final["cart_total_yen"] != cart.get("cart_total_yen"):
            raise RuntimeError(
                f"直前で金額が変わったよ（{cart.get('cart_total_yen')}円 → "
                f"{final['cart_total_yen']}円）。中止したよ。")

        page.get_by_role("button", name=PAY_BUTTON).first.click(timeout=15000)

        # From this line on, money may have moved. Nothing below is allowed
        # to raise: an exception here would leave the job in "verifying"
        # with no announcement and no verdict, which is the worst of all
        # worlds -- charged, silent, and unrecorded. Whatever goes wrong
        # reads as UNKNOWN and goes to a person.
        try:
            outcome = _settle_after_click(page, before)
        except Exception as exc:  # noqa: BLE001 -- deliberate: see above
            outcome = {"payment_executed": True, "dry_run": False,
                       "outcome": "UNKNOWN",
                       "note": "決済ボタンは押したけど、そのあと画面が読めなくなったよ。"
                               "アプリの注文履歴を確認してね。二重に押すことはしないよ。"}
        try:
            job_dir = config.JOBS_DIR / job["job_id"]
            job_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(job_dir / "paid.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        if outcome["outcome"] == "PAID":
            outcome["verified_total_yen"] = final["cart_total_yen"]
        return outcome


# A payment is given this long to settle before the answer becomes "a person
# looks". The old code slept 8 seconds and read once, which classified any
# slower success as NOT_PLACED -- and NOT_PLACED is the one verdict that
# invites the user to order again, so a slow success plus an obedient user
# equals two coffees. Sixty seconds of polling makes that window as small
# as patience allows; what is still unreadable after that was never going
# to be settled by waiting.
SETTLE_DEADLINE_SECONDS = 60.0
SETTLE_POLL_SECONDS = 2.0


def _settle_after_click(page: Any, before: str) -> dict[str, Any]:
    """Watch the page until the payment admits what it did.

    Polls rather than sleeping once: an order number can take its time
    arriving, and the cost of looking again is nothing next to the cost of
    the wrong verdict. Individual reads are allowed to fail (the page
    navigates mid-read); a read that fails is just "not settled yet".
    """
    deadline = time.monotonic() + SETTLE_DEADLINE_SECONDS
    last_seen = before
    while time.monotonic() < deadline:
        time.sleep(SETTLE_POLL_SECONDS)
        try:
            current = page.inner_text("body")
        except Exception:  # noqa: BLE001 -- mid-navigation; look again
            continue
        last_seen = current
        number = _ORDER_NUMBER_RE.search(current)
        if number:
            return {"payment_executed": True, "dry_run": False,
                    "outcome": "PAID", "order_number": number.group(1)}
    return _settlement_verdict(before, last_seen)


def _settlement_verdict(before: str, last_seen: str) -> dict[str, Any]:
    """The deadline verdict, from the last page anyone could read.

    NOT_PLACED is only claimed when the page never changed at all: same
    screen, same button, nothing moved for the whole deadline. That is what
    an ignored click looks like. A page that changed in any way -- error
    text, a spinner, a partial navigation -- might be a payment in flight,
    and claiming "nothing happened" about it is exactly the misdiagnosis
    that invites a double order. Everything that moved reads as UNKNOWN.
    """
    if last_seen == before:
        return {"payment_executed": False, "dry_run": False,
                "outcome": "NOT_PLACED",
                "note": "決済画面のまま何も変わらなかったよ。注文は成立していないはず。"}
    return {"payment_executed": True, "dry_run": False,
            "outcome": "UNKNOWN",
            "note": "決済したけど注文番号が読み取れなかったよ。"
                    "アプリの注文履歴を確認してね。二重に押すことはしないよ。"}
