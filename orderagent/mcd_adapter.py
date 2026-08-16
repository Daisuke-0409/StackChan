"""McDonald's web mobile-order automation (FR-4) -- cart building only.

This module NEVER clicks a payment control. It builds the cart, scrapes what
the site itself says the cart contains, screenshots every step, and stops.
The payment step lives in payment.py behind its own guard, and takes the
scraped cart -- not this module's intentions -- as its input.

The web order UI only exists for mobile user agents (verified 2026-08-16:
desktop UAs get a marketing page with no entry point), so the browser context
is a phone. All timing is human-speed on purpose (ToS §7).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from . import config

ORDER_URL = "https://www.mcdonalds.co.jp/order/{key}"
PRODUCT_URL = "https://www.mcdonalds.co.jp/order/{key}/products/{pid}"

_CAPTCHA_MARKERS = ["recaptcha", "hcaptcha", "captcha", "私はロボットではありません"]
_YEN_RE = re.compile(r"[¥￥]\s*([0-9,]+)")


class EscalationNeeded(RuntimeError):
    """The site asked for something automation must not do (CAPTCHA,
    login wall, unexpected dialog). The job stops and a human takes over."""


def _sleep() -> None:
    time.sleep(config.ACTION_DELAY_SECONDS)


def _check_for_captcha(page: Any) -> None:
    content = page.content().lower()
    for marker in _CAPTCHA_MARKERS:
        if marker in content:
            raise EscalationNeeded(f"bot check detected: {marker}")


# Store-notice modals ("ドライブスルーでの受け取りはできません" etc.) sit
# over the whole page and swallow every click until acknowledged -- exactly
# what a person would tap through. Only these known continue/close labels
# are ever clicked; an unrecognized dialog stays up and surfaces as a
# timeout, which is the correct failure for a page we don't understand.
_NOTICE_BUTTONS = re.compile(r"^(注文を続ける|OK|ＯＫ|閉じる|確認|了解)$")


def _dismiss_notices(page: Any) -> None:
    for _ in range(3):
        button = page.get_by_role("button", name=_NOTICE_BUTTONS)
        try:
            if button.count() == 0 or not button.first.is_visible():
                return
            button.first.click(timeout=3000)
            _sleep()
        except Exception:
            return


_CLEAR_CONFIRM = re.compile(r"^(削除する|削除|はい|OK|ＯＫ)$")


def _clear_cart(page: Any, job_dir: Path) -> None:
    """Empties any leftover cart on the listing page. Idempotent."""
    clear = page.get_by_role("button", name="全て削除")
    if clear.count() == 0 or not clear.first.is_visible():
        return
    clear.first.click(timeout=5000)
    _sleep()
    confirm = page.get_by_role("button", name=_CLEAR_CONFIRM)
    if confirm.count() and confirm.first.is_visible():
        confirm.first.click(timeout=5000)
        _sleep()
    if page.get_by_role("button", name="全て削除").count() and \
            page.get_by_role("button", name="全て削除").first.is_visible():
        _shot(page, job_dir, "cart_clear_failed")
        raise EscalationNeeded("既存カートを空にできませんでした")


def _shot(page: Any, job_dir: Path, name: str) -> None:
    try:
        page.screenshot(path=str(job_dir / f"{name}.png"), full_page=False)
    except Exception:
        pass  # a failed screenshot must not fail the job


def build_cart(store_key: str, items: list[dict[str, Any]], job_id: str,
               log: Callable[[str, dict], None]) -> dict[str, Any]:
    """Adds `items` ([{id, name, quantity}]) to the store's cart.

    Returns {"cart_items": [...], "cart_total_yen": int, "screenshots": dir}.
    Raises EscalationNeeded when a human has to take over. The returned cart
    is what the SITE says, scraped from the cart page -- payment.py compares
    it against the approved order and refuses on any mismatch.
    """
    from playwright.sync_api import sync_playwright  # deferred: heavy import

    job_dir = config.JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.PROFILE_DIR),
            headless=True,
            user_agent=config.MOBILE_UA,
            viewport=config.VIEWPORT,
            locale="ja-JP",
            is_mobile=True,
            has_touch=True,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # The cart survives across browser launches (server/cookie side,
            # observed 2026-08-16: a hamburger from a previous session doubled
            # the total). Every job therefore starts by emptying it -- cart
            # verification is meaningless over leftovers.
            page.goto(ORDER_URL.format(key=store_key),
                      wait_until="networkidle", timeout=45000)
            _check_for_captcha(page)
            _sleep()
            _dismiss_notices(page)
            _clear_cart(page, job_dir)

            for index, item in enumerate(items):
                for _ in range(int(item.get("quantity", 1))):
                    # In-app navigation, NOT page.goto(): the cart lives only
                    # in the SPA's memory, and a full reload between products
                    # silently drops everything added so far (observed
                    # 2026-08-16 -- the first item vanished from a two-item
                    # order). pushState + popstate walks the router the way
                    # a tap on a product card would.
                    page.evaluate(_NAV_JS, f"/order/{store_key}/products/{item['id']}")
                    page.wait_for_timeout(1500)
                    _check_for_captcha(page)
                    _dismiss_notices(page)
                    add = page.get_by_role("button", name=re.compile("カートに追加"))
                    if add.count() == 0:
                        _shot(page, job_dir, f"item{index}_no_add_button")
                        raise EscalationNeeded(
                            f"「カートに追加」が見つかりません: {item['name']}")
                    add.first.click(timeout=15000)
                    log("cart_add_clicked", {"item": item["name"]})
                    _sleep()
                _shot(page, job_dir, f"item{index}_added")

            # The checkout sheet, reached the way a person reaches it. There
            # is no /cart URL -- レジに進む opens ご注文内容の確定 in place
            # (verified 2026-08-16; a guessed URL just re-renders the menu).
            checkout = page.get_by_role("button", name="レジに進む")
            if checkout.count() == 0:
                _shot(page, job_dir, "no_checkout_button")
                raise EscalationNeeded("「レジに進む」が見つかりません（カートが空？）")
            checkout.first.click(timeout=15000)
            page.wait_for_selector("text=ご注文内容の確定", timeout=15000)
            _sleep()
            _shot(page, job_dir, "checkout")
            cart = _scrape_checkout(page)
            log("cart_scraped", cart)
            if not cart["cart_items"]:
                raise EscalationNeeded("ご注文内容の確定ページから明細を読み取れませんでした")
            cart["screenshots"] = str(job_dir)
            return cart
        finally:
            context.close()


# SPA route change without a reload -- the way every in-cart navigation
# must happen (see the comment at the add loop).
_NAV_JS = """
(url) => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')); }
"""

# Each cart line carries its own 変更 button, and the page renders the whole
# cart list TWICE (two sibling ULs with identical rows -- reading both
# double-counted every item and every yen until 2026-08-16). Rows are
# grouped by their ancestor UL and only the first list is returned.
_ROWS_JS = """
() => {
  const uls = new Map();
  document.querySelectorAll('button').forEach((b) => {
    if (b.innerText.trim() !== '変更') return;
    let el = b.parentElement, row = null, ul = null;
    for (let k = 0; k < 8 && el; k++, el = el.parentElement) {
      if (!row && /[\\u00A5\\uFFE5]\\s*[0-9,]+/.test(el.innerText)) row = el;
      if (el.tagName === 'UL') { ul = el; break; }
    }
    if (!row) return;
    const key = ul || document.body;
    if (!uls.has(key)) uls.set(key, []);
    uls.get(key).push(row.innerText);
  });
  const lists = [...uls.values()];
  return lists.length ? lists[0] : [];
}
"""


def _scrape_checkout(page: Any) -> dict[str, Any]:
    """Line items exactly as ご注文内容の確定 displays them.

    Returns {"cart_items": [{name, price, quantity}], "cart_total_yen": int,
    "pickup_options": [...]}. The total is the sum of displayed unit prices
    times displayed quantities -- the page shows no grand total until a
    receive method is chosen, and choosing one is a state change that
    belongs to the payment walkthrough, not to cart verification.
    """
    row_texts = page.evaluate(_ROWS_JS)
    cart_items = []
    for row in row_texts:
        lines = [ln.strip() for ln in row.splitlines() if ln.strip()]
        name = next((ln for ln in lines
                     if ln not in ("変更",) and not _YEN_RE.search(ln)
                     and not re.fullmatch(r"[0-9０-９]+", ln)
                     and not re.fullmatch(r"[-−+＋]", ln)), None)
        price_match = next((_YEN_RE.search(ln) for ln in lines if _YEN_RE.search(ln)), None)
        quantity = next((int(ln) for ln in lines if re.fullmatch(r"[0-9]+", ln)), 1)
        if name and price_match:
            cart_items.append({"name": name,
                               "price": int(price_match.group(1).replace(",", "")),
                               "quantity": quantity})
    total = sum(item["price"] * item["quantity"] for item in cart_items) or None

    body = page.inner_text("body")
    pickup_options = [label for marker, label in
                      (("テイクアウト", "takeout"), ("店内でお食事", "eatin"),
                       ("ドライブスルー", "drive_through"), ("駐車場で受け取る", "park_and_go"))
                      if marker in body]
    return {"cart_items": cart_items, "cart_total_yen": total,
            "pickup_options": pickup_options}
