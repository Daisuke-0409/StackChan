"""Order intent parsing (FR-1) -- deliberately conservative.

An utterance either parses into an unambiguous order request or it doesn't;
anything unclear becomes a question back to the user, never a guess. Money is
downstream of this parse, so the bias is 聞き返し over 推測 everywhere.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

CHAIN_WORDS = {
    "mcd": ["マクドナルド", "マクド", "マック"],
    # Adapters for these don't exist yet; naming them still gets a clear
    # "まだ対応していない" reply instead of silence.
    "mos": ["モスバーガー", "モス"],
    "kfc": ["ケンタッキー", "ケンタ"],
    "starbucks": ["スターバックス", "スタバ"],
}
ORDER_WORDS = ["モバイルオーダー", "注文", "オーダー", "頼んで", "買って", "テイクアウト"]

PICKUP_WORDS = {
    "drive_through": ["ドライブスルー"],
    "takeout": ["持ち帰り", "テイクアウト", "お持ち帰り"],
    "eatin": ["店内", "イートイン"],
}

_COUNT_RE = re.compile(r"([0-9０-９]+)\s*(?:個|つ|杯|点)")


@dataclass
class OrderIntent:
    chain: str
    item_text: str                       # what's left to match against the menu
    quantity: int = 1
    pickup: Optional[str] = None         # None = ask
    missing: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def detect(text: str) -> Optional[OrderIntent]:
    """OrderIntent if this utterance asks for a mobile order, else None.

    Requires BOTH a chain word and an order word: "マックってどこ？" or
    "注文の仕方教えて" alone must not start a job that ends in a payment
    gate.
    """
    normalized = _normalize(text)
    chain = None
    for chain_id, words in CHAIN_WORDS.items():
        if any(w in normalized for w in words):
            chain = chain_id
            break
    if chain is None or not any(w in normalized for w in ORDER_WORDS):
        return None

    pickup = None
    for method, words in PICKUP_WORDS.items():
        if any(w in normalized for w in words):
            pickup = method
            break

    quantity = 1
    count = _COUNT_RE.search(normalized)
    if count:
        quantity = int(unicodedata.normalize("NFKC", count.group(1)))

    # Strip chain/order/pickup words; what remains is the item description.
    item_text = normalized
    strip_words = (CHAIN_WORDS[chain] + ORDER_WORDS
                   + [w for ws in PICKUP_WORDS.values() for w in ws]
                   + ["で", "を", "して", "お願い", "ちょうだい", "くれ"])
    for word in sorted(strip_words, key=len, reverse=True):
        item_text = item_text.replace(word, " ")
    if count:
        item_text = item_text.replace(count.group(0), " ")
    item_text = re.sub(r"\s+", " ", item_text).strip()

    missing = []
    if not item_text:
        missing.append("item")
    if pickup is None:
        missing.append("pickup")
    if not 1 <= quantity <= 10:  # a mis-heard number must not become 100 burgers
        missing.append("quantity")
        quantity = 1
    return OrderIntent(chain=chain, item_text=item_text, quantity=quantity,
                       pickup=pickup, missing=missing)


# --- menu matching ----------------------------------------------------------

_SIZE_MAP = {"エス": "S", "エム": "M", "エル": "L", "s": "S", "m": "M", "l": "L"}

_ITEM_SPLIT_RE = re.compile(r"\s*(?:と|、|,)\s*")


def split_items(item_text: str) -> list[str]:
    """"ハンバーガーとポテト" -> ["ハンバーガー", "ポテト"]. Empty parts drop."""
    return [part for part in _ITEM_SPLIT_RE.split(item_text) if part.strip()]


def match_menu(item_text: str, menu_items: list[dict]) -> list[dict]:
    """Menu entries whose name matches item_text, best first.

    Matching is by normalized substring both ways, with exact name matches
    first, then shortest name (the plain item rather than its セット variant)
    so "アイスコーヒー" prefers アイスコーヒー(S/M/L) over セット商品.
    A spoken size letter ("アイスコーヒーエル") is folded to L before
    matching.
    """
    query = _normalize(item_text).strip()
    for spoken, letter in _SIZE_MAP.items():
        query = re.sub(spoken + r"$", letter, query, flags=re.IGNORECASE)
    query_compact = re.sub(r"[\s()（）®]", "", query).lower()
    if not query_compact:
        return []
    scored = []
    for entry in menu_items:
        name_compact = re.sub(r"[\s()（）®]", "", _normalize(entry["name"])).lower()
        if query_compact == name_compact:
            score = (0, len(name_compact))
        elif query_compact in name_compact:
            score = (1, len(name_compact))
        elif name_compact in query_compact:
            score = (2, len(name_compact))
        else:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0])
    return [entry for _, entry in scored]
