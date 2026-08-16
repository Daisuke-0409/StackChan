"""The payment gate (FR-5 / NFR-1). Nothing in this file is best-effort.

Layers, all of which must pass before anything is clicked:
1. ORDER_PAYMENT_ENABLED=1 at process start -- otherwise every job is a dry
   run, unconditionally. Cannot be flipped over HTTP.
2. The job was explicitly approved through the voice gate.
3. verify_cart(): the site's own cart (scraped) matches the approved order --
   item count and exact total. Any mismatch aborts.
4. The total is within MAX_ORDER_YEN.
5. One attempt, ever. An ambiguous outcome (timeout mid-payment) marks the
   job payment_uncertain and STOPS; a human checks the order history. There
   is no retry path in this file by design -- double payment is the one
   failure worse than no payment.
"""
from __future__ import annotations

from typing import Any

from . import config


class PaymentRefused(RuntimeError):
    """A guard failed. The message says which one; nothing was clicked."""


def verify_cart(approved: dict[str, Any], cart: dict[str, Any]) -> None:
    """The approved order vs what the site's cart page displays.

    `approved` is what was read aloud and confirmed: expected_total_yen and
    expected_item_count. Raises PaymentRefused on ANY discrepancy -- a
    mis-scrape and a real mismatch are treated identically, because from
    here they are indistinguishable and both mean: do not pay.
    """
    cart_total = cart.get("cart_total_yen")
    if cart_total is None:
        raise PaymentRefused("カート合計が読み取れませんでした")
    if cart_total != approved["expected_total_yen"]:
        raise PaymentRefused(
            f"承認額 {approved['expected_total_yen']}円 とカート表示 {cart_total}円 が一致しません")
    if cart_total > config.MAX_ORDER_YEN:
        raise PaymentRefused(
            f"合計 {cart_total}円 が上限 {config.MAX_ORDER_YEN}円 を超えています")
    expected_count = approved.get("expected_item_count")
    if expected_count is not None:
        actual = len(cart.get("cart_items") or [])
        if actual != expected_count:
            raise PaymentRefused(
                f"承認された商品数 {expected_count} に対しカートは {actual} 点です")

    # Item-level: every approved line must exist in the cart with the same
    # displayed price and quantity. Names are compared loosely (the site
    # decorates with ® and spacing) but price and quantity exactly.
    def _fold(name: str) -> str:
        import re as _re
        import unicodedata as _u
        return _re.sub(r"[\s()（）®]", "", _u.normalize("NFKC", name)).lower()

    cart_lines = list(cart.get("cart_items") or [])
    for wanted in approved.get("expected_items") or []:
        found = next((line for line in cart_lines
                      if line.get("price") == wanted["price"]
                      and line.get("quantity", 1) == wanted.get("quantity", 1)
                      and (_fold(wanted["name"]) in _fold(line.get("name", ""))
                           or _fold(line.get("name", "")) in _fold(wanted["name"]))), None)
        if found is None:
            raise PaymentRefused(
                f"承認された「{wanted['name']} {wanted.get('quantity', 1)}点 "
                f"{wanted['price']}円」がカート明細に見つかりません")
        cart_lines.remove(found)


def execute(job: dict[str, Any], cart: dict[str, Any]) -> dict[str, Any]:
    """The only function allowed to move money, and today it doesn't.

    With ORDER_PAYMENT_ENABLED unset this verifies everything and returns a
    dry-run result. The live path is intentionally NOT implemented until the
    payment method exists in the browser profile (entered by hand) and the
    checkout flow has been walked through together on a real 少額 order --
    implementing it blind against an unseen payment page would be exactly
    the kind of guesswork this file exists to forbid.
    """
    verify_cart(job["approved"], cart)

    if not config.PAYMENT_ENABLED:
        return {
            "payment_executed": False,
            "dry_run": True,
            "verified_total_yen": cart["cart_total_yen"],
            "note": "ORDER_PAYMENT_ENABLED が無効のためドライラン。検証はすべて通過。",
        }

    # Live payment: not yet implemented, deliberately. Refusing is the safe
    # behavior -- a job reaching here with the flag on still must not click
    # controls this code has never seen.
    raise PaymentRefused(
        "決済フローは実サイトでの共同ウォークスルー後に実装されます（現在は未実装）")
