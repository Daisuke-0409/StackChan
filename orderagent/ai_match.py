"""AI menu matching -- for the words people actually say.

Nobody orders a プレミアムローストアイスコーヒー by its full name; they say
アイスコーヒー or ダブチ. Exact/substring matching (intent.match_menu) stays
the first path because it is free and deterministic; this module is the
fallback that asks Gemini to read the store's real menu and guess what was
meant. The guess is never silently trusted with money: the approval
readback names the resolved product and its price, and 「キャンセル」 kills
it -- same gate as everything else.

Uses the gateway's Gemini key (shared .env). No key or no network simply
returns [], and the caller falls back to 聞き返し.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any, Optional

_MODEL = "gemini-3.5-flash-lite"
_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        f"{_MODEL}:generateContent")

_PROMPT = (
    "あなたはファストフードの注文アシスタントです。"
    "客が言った商品名(通称・略称・言い間違いを含む)に該当しそうな商品を、"
    "以下の実メニューから最大3件、可能性の高い順に選んでください。"
    "メニューにない名前を作ってはいけません。該当が本当に無ければ空配列。"
    'JSONのみで答える: {"candidates": ["商品名", ...]}\n'
    "客の発話: {spoken}\n"
    "メニュー: {menu}"
)


def _parse_response(body: dict[str, Any]) -> list[str]:
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        names = json.loads(text).get("candidates", [])
        return [n for n in names if isinstance(n, str)]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return []


def suggest(spoken: str, menu_items: list[dict[str, Any]],
            timeout: float = 15.0) -> list[dict[str, Any]]:
    """Menu entries Gemini thinks `spoken` refers to, best first.

    Returned entries are always taken from `menu_items` by exact name --
    the model chooses, the menu supplies the id and price. Anything the
    model invents that isn't on the menu is dropped.
    """
    key = os.environ.get("AI_PROVIDER_API_KEY", "")
    if not key or not spoken.strip() or not menu_items:
        return []
    names = [item["name"] for item in menu_items]
    prompt = _PROMPT.replace("{spoken}", spoken).replace("{menu}", "、".join(names))
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.1},
    }
    request = urllib.request.Request(
        f"{_URL}?key={key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl.create_default_context()) as response:
            body = json.loads(response.read())
    except Exception:  # noqa: BLE001 -- no AI just means no suggestion
        return []
    by_name = {item["name"]: item for item in menu_items}
    return [by_name[name] for name in _parse_response(body) if name in by_name]
