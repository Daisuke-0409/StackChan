"""McDonald's Japan store search -- no Google Places, no API key.

Discovered live on 2026-08-16:
- https://map.mcdonalds.co.jp/api/poi returns every store in the country
  (~2100 entries) as JSON: {key, name, latitude, longitude, address, ...}.
  `key` is the store number that /order/<key> takes.
- Store data (hours, mopEnabled) lives at
  https://data.cat.group-<X>.prod.mop.mcd.qorcommerce.com/<key>.json where
  the shard letter <X> differs per store; the order page HTML for the store
  names its shard, so it is resolved from there and cached.
- Menu: .../<key>/menu.json on the same shard host maps product names to the
  product ids that /order/<key>/products/<pid> takes.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from typing import Any, Optional

from . import config

POI_URL = "https://map.mcdonalds.co.jp/api/poi"
# A DEEP route, deliberately: /order/<key> itself 302s to the marketing page
# since 2026-08-19, but deeper SPA paths still serve the shard-bearing
# bootstrap HTML. Routing is client-side, so the product id need not exist;
# 1010 (ハンバーガー) is used because it happens to be real everywhere.
ORDER_PAGE = "https://www.mcdonalds.co.jp/order/{key}/products/1010"
DATA_HOST = "https://data.cat.group-{shard}.prod.mop.mcd.qorcommerce.com"
_POI_CACHE_SECONDS = 24 * 3600
_SHARD_RE = re.compile(r"group-([a-z0-9]+)")


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": config.MOBILE_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _poi_cache_path():
    return config.DATA_DIR / "mcd_poi.json"


def all_stores(refresh: bool = False) -> list[dict[str, Any]]:
    """The national store list, cached for a day."""
    config.ensure_dirs()
    cache = _poi_cache_path()
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < _POI_CACHE_SECONDS:
        return json.loads(cache.read_text(encoding="utf-8"))
    stores = json.loads(_fetch(POI_URL).decode("utf-8"))
    cache.write_text(json.dumps(stores, ensure_ascii=False), encoding="utf-8")
    return stores


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rad = math.radians
    d_lat = rad(lat2 - lat1)
    d_lng = rad(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(d_lng / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def nearest(lat: float, lng: float, limit: int = 3) -> list[dict[str, Any]]:
    """The `limit` nearest stores to (lat, lng), each with distance_km added."""
    ranked = sorted(
        ({**s, "distance_km": round(_haversine_km(lat, lng, s["latitude"], s["longitude"]), 2)}
         for s in all_stores()),
        key=lambda s: s["distance_km"])
    return ranked[:limit]


# --- per-store data shard ---------------------------------------------------

def _shard_cache_path():
    return config.DATA_DIR / "mcd_shards.json"


def _load_shards() -> dict[str, str]:
    path = _shard_cache_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def resolve_shard(store_key: str) -> Optional[str]:
    """The data-host shard letter for a store, from its order page HTML."""
    shards = _load_shards()
    if store_key in shards:
        return shards[store_key]
    html = _fetch(ORDER_PAGE.format(key=store_key)).decode("utf-8", "replace")
    match = _SHARD_RE.search(html)
    if not match:
        return None
    shards[store_key] = match.group(1)
    config.ensure_dirs()
    _shard_cache_path().write_text(json.dumps(shards), encoding="utf-8")
    return match.group(1)


def store_detail(store_key: str) -> Optional[dict[str, Any]]:
    """Hours / mopEnabled / name for one store, or None if unavailable."""
    shard = resolve_shard(store_key)
    if not shard:
        return None
    raw = json.loads(_fetch(f"{DATA_HOST.format(shard=shard)}/{store_key}.json").decode("utf-8"))
    return raw.get("store")


def menu(store_key: str) -> list[dict[str, Any]]:
    """The store's menu as a flat [{id, name, price}] list.

    menu.json splits the catalog: prices sit in top-level `products` keyed by
    productCode, Japanese names in `groupMenu.products.<code>.tName.ja`
    (verified live against 45520 on 2026-08-16). Only codes present in BOTH
    are returned -- a name without a price is not orderable, and the id is
    exactly what /order/<key>/products/<id> takes.
    """
    shard = resolve_shard(store_key)
    if not shard:
        return []
    raw = json.loads(_fetch(f"{DATA_HOST.format(shard=shard)}/{store_key}/menu.json").decode("utf-8"))
    priced = raw.get("products") or {}
    named = ((raw.get("groupMenu") or {}).get("products")) or {}
    items: list[dict[str, Any]] = []
    for code, entry in named.items():
        name = ((entry.get("tName") or {}).get("ja") or "").strip()
        price_info = priced.get(code)
        if not name or not price_info:
            continue
        price = (price_info.get("price") or {}).get("price")
        if not price:  # 0 or missing: composition parts, not orderable items
            continue
        items.append({"id": str(code), "name": name, "price": int(price)})
    return items
