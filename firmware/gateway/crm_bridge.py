"""Answers grave-location questions through the office relay (R6).

The other half of crm_relay.py: that process sits on the office PC and is
the only thing allowed to talk to the CRM; this module is the gateway's
client for it. The division of responsibilities is deliberate and the
opposite of the relay's -- everything about *conversation* lives here:
whether an utterance is a CRM question, what the robot says for each shape
of answer, and the rule that none of it is remembered.

Privacy rules, enforced by structure rather than intention:
- A recognized CRM question never reaches Gemini, even when the lookup
  fails -- the question contains a customer's name, and 顧客データを
  Gemini に渡さない is a hard line, not a preference.
- The exchange is never fed to _remember_exchange. The CRM's audit log is
  the record of who asked; the robot's memory must not be a second ledger.
- Log lines carry counts and status codes, never the name asked about.

Configuration (gateway/.env):
    CRM_RELAY_URL=http://100.76.60.88:8767      # the office PC, over Tailscale
    CRM_RELAY_TOKEN=...                          # matches the relay's own token

With no URL configured the bridge answers None to everything and chat
behaves as if this module did not exist.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

_TIMEOUT_SECONDS = 15.0

# A grave question is NAME + さん/様 + a grave word, with a question word
# somewhere -- all three, because the payment-adjacent lesson generalizes:
# an interceptor this close to customer data must never fire on small talk
# ("お墓参りどこ行く？" has a grave word and a question word, but no named
# person, and stays ordinary conversation).
_NAME_GRAVE_RE = re.compile(
    r"([^\s。、,，！？!?の]{1,12})(?:さん|様)の(?:お墓|墓所|墓|霊園|区画)")
_QUESTION_RE = re.compile(r"どこ|何処|どちら|場所|教えて|調べて|わかる|分かる")
# Self-references make grim jokes, not lookups.
_NOT_A_CUSTOMER = ("俺", "私", "僕", "自分", "うち", "わたし", "おれ", "ぼく")


def detect(text: str) -> Optional[str]:
    """The customer name if this utterance asks where someone's grave is."""
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    match = _NAME_GRAVE_RE.search(normalized)
    if not match or not _QUESTION_RE.search(normalized):
        return None
    name = match.group(1).strip()
    if not name or name in _NOT_A_CUSTOMER:
        return None
    return name


def _format_grave(row: dict[str, Any]) -> str:
    """「郡司分地区の専唱寺」 / 「みたまA-12」 -- the contract's spoken form.

    The 区画 number lives inside cemetery_name; `area` is a district and is
    empty for 36% of real graves, which is normal and must not be narrated
    as missing. zenrin_map_no never reaches this process at all.
    """
    cemetery = (row.get("cemetery_name") or "").strip()
    area = (row.get("area") or "").strip()
    if area:
        return f"{area}地区の{cemetery}"
    return cemetery


def _default_fetch(url: str, token: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, headers={"X-Tachikoma-Token": token, "Accept": "application/json"},
        method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _compose_reply(name: str, total: int, rows: list[dict[str, Any]]) -> str:
    if total == 0:
        return f"{name}さんのお墓は、台帳には見つからなかったよ。"
    if total == 1 and rows:
        return f"{name}さんのお墓は、{_format_grave(rows[0])}だよ。"
    parts = [f"{name}さんのお墓は{total}件あるよ。"]
    for row in rows:
        owner = (row.get("customer_name") or name).strip()
        parts.append(f"{owner}さんが{_format_grave(row)}。")
    if total > len(rows):
        parts.append("多いから、下の名前も付けてもう一度聞いてね。")
    return "".join(parts)


def intercept(text: str, asked_by: str,
              fetch: Optional[Callable[[str, str], tuple[int, dict[str, Any]]]] = None,
              env: Optional[dict[str, str]] = None) -> Optional[str]:
    """A spoken reply if this is a grave question, else None.

    Once a question is recognized it is answered here in every case,
    including every failure case: falling through to normal chat would hand
    the customer's name to Gemini, so there is deliberately no path that
    does. The caller must not remember the exchange.
    """
    env = env if env is not None else os.environ
    relay_url = (env.get("CRM_RELAY_URL") or "").rstrip("/")
    if not relay_url:
        return None
    name = detect(text)
    if name is None:
        return None

    import urllib.parse
    query = urllib.parse.urlencode({"name": name, "asked_by": asked_by or "unknown"})
    fetch = fetch or _default_fetch
    try:
        status, payload = fetch(f"{relay_url}/crm/lookup?{query}",
                                env.get("CRM_RELAY_TOKEN", ""))
    except urllib.error.HTTPError as exc:
        _log_status(exc.code)
        if exc.code == 403:
            return "台帳の鍵が合わなかったよ。設定を確認してもらってね。"
        if exc.code in (502, 503):
            return "会社のシステムが台帳を引けない状態みたい。会社のパソコンとCRMを見てもらってね。"
        return "台帳の照会でエラーが出たよ。"
    except Exception:  # noqa: BLE001 -- unreachable office PC is an expected state
        _log_status(None)
        return "会社のパソコンに繋がらなくて、台帳が引けないよ。会社が開いている時間なら、パソコンの電源を確認してね。"

    _log_status(status, payload.get("count"))
    total = int(payload.get("count", 0))
    rows = payload.get("results") or []
    return _compose_reply(name, total, rows)


def _log_status(status: Optional[int], count: Optional[int] = None) -> None:
    # The shape of the traffic, never its subject.
    print(f"[crm_bridge] lookup status={status} count={count}", flush=True)
