"""Headful browser on the order agent's own profile -- for the human steps.

Two jobs, both Daisuke's hands and never the agent's:
1. Payment registration: log into PayPay (or enter a card) INSIDE this
   browser, so the credential session lives in orderagent/data/browser_profile
   and the agent never sees the values.
2. The checkout walkthrough: step through 受け取り方法 -> 支払い方法 up to
   the final confirm screen, so the flow can be mapped together.

The script builds a small cart first (default: coffee S at the default
store) purely to save taps, then hands over. It NEVER clicks past the
checkout sheet, and closing the window ends it.

    python -m orderagent.manual_browser [store_key] [product_id]
"""
from __future__ import annotations

import sys
import time

from . import config, mcd_adapter


def main() -> None:
    from playwright.sync_api import sync_playwright

    store_key = sys.argv[1] if len(sys.argv) > 1 else "45520"
    product_id = sys.argv[2] if len(sys.argv) > 2 else "3547"  # アイスコーヒー(S)

    config.ensure_dirs()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.PROFILE_DIR),
            channel=config.BROWSER_CHANNEL,
            headless=False,
            user_agent=config.MOBILE_UA,
            viewport=config.VIEWPORT,
            locale="ja-JP",
            is_mobile=True,
            has_touch=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"https://www.mcdonalds.co.jp/order/{store_key}",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        mcd_adapter._dismiss_notices(page)
        page.evaluate(mcd_adapter._NAV_JS, f"/order/{store_key}/products/{product_id}")
        page.wait_for_timeout(1500)
        mcd_adapter._dismiss_notices(page)
        page.get_by_role("button", name="カートに追加").first.click(timeout=15000)
        page.wait_for_timeout(1200)
        page.get_by_role("button", name="レジに進む").first.click(timeout=15000)
        print("ここから先は人間の手番です。ウィンドウを閉じると終了します。", flush=True)

        try:
            while context.pages:
                time.sleep(1)
        except Exception:
            pass
        context.close()


if __name__ == "__main__":
    main()
