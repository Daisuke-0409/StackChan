"""Gateway-side bridge to the order agent (モバイルオーダー).

The gateway's only jobs here: recognize an order utterance, forward it to
the order agent (127.0.0.1:8766), and route the follow-up approval or
denial. Everything heavy -- store search, Playwright, the payment gate --
lives in the agent process, so this module is stdlib-only and every call
into it fails soft: if the agent is down, chat behaves exactly as before.

Approval phrases are intentionally stricter than the R4 tool-approval
gate's: money is on the other side, so a bare はい does NOT approve an
order -- the word 注文 (or キャンセル to stop) must be spoken.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

ORDER_AGENT_URL = "http://127.0.0.1:8766"
_TIMEOUT = 3.0          # control calls; the agent answers on loopback
_PENDING_TIMEOUT = 0.8  # per-utterance check; must never stall normal chat

# Explicit, order-specific wording only. 「注文して」「注文いいよ」「注文オッケー」…
_APPROVE_RE = re.compile(r"注文\s*(して|していい|お願い|おねがい|いいよ|オッケー|オーケー|ok|OK|確定)|はい[、。\s]*注文")
_DENY_RE = re.compile(r"キャンセル|(注文)?\s*(やめて|やめとく|しないで|中止|なし)|だめ")

# orderagent.intent is pure stdlib; the repo root is two levels up from
# firmware/gateway/. Import failure just disables the feature.
try:
    _REPO_ROOT = str(Path(__file__).resolve().parents[2])
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from orderagent import intent as _intent
except Exception:  # noqa: BLE001
    _intent = None


def _agent(method: str, path: str, payload: Optional[dict] = None,
           timeout: float = _TIMEOUT) -> Optional[dict[str, Any]]:
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(ORDER_AGENT_URL + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001 -- agent down = feature off, never an error
        return None


def intercept(text: str, device_id: str,
              location: Optional[dict[str, float]] = None) -> Optional[str]:
    """A spoken reply if this utterance belongs to the order flow, else None.

    Checked BEFORE the LLM sees the utterance, in two steps:
    1. If an order is awaiting approval, this utterance is judged as
       approve / deny / neither. Neither passes through to normal chat --
       an unrelated question during the approval window stays answerable,
       and the 5-minute expiry handles silence.
    2. Otherwise, order-intent detection.
    """
    if _intent is None:
        return None

    pending = _agent("GET", "/pending", timeout=_PENDING_TIMEOUT)
    if pending and pending.get("pending"):
        job_id = pending["pending"]["job_id"]
        if _APPROVE_RE.search(text):
            result = _agent("POST", f"/jobs/{job_id}/approve", {"phrase": text})
            if result is None:
                return "ごめん、注文システムに繋がらなかった。もう一度言ってみて。"
            # The agent announces the outcome itself; acknowledge briefly.
            return "承認を受け付けたよ。"
        if _DENY_RE.search(text):
            result = _agent("POST", f"/jobs/{job_id}/deny", {"phrase": text})
            if result is None:
                return "ごめん、注文システムに繋がらなかった。もう一度言ってみて。"
            return "注文はキャンセルしておいたよ。"
        return None  # unrelated talk during the window -> normal chat

    order = _intent.detect(text)
    if order is None:
        return None
    if "item" in order.missing:
        return "何を注文する？商品名を教えて。"
    payload: dict[str, Any] = {
        "chain": order.chain,
        "item_text": order.item_text,
        "quantity": order.quantity,
        "pickup": order.pickup,
        "device_id": device_id,
    }
    if location:
        payload["lat"] = location.get("lat")
        payload["lng"] = location.get("lng")
    result = _agent("POST", "/jobs", payload)
    if result is None:
        return "注文システムが起動してないみたい。orderagentを立ち上げてね。"
    if "error" in result:
        return result.get("message", "今は注文を受け付けられないよ。")
    return "了解、注文を準備するね。内容がまとまったら読み上げるから待ってて。"
