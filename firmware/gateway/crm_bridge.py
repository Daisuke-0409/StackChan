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

A lookup can take two turns. "6件あるよ" is not an answer, so the bridge
remembers -- in this process only, for two minutes -- that it is waiting
for a given name, and reads the next short utterance as that name rather
than as conversation. Nothing about the wait is written to memory or to a
log; it exists in RAM and expires.

With no URL configured the bridge answers None to everything and chat
behaves as if this module did not exist.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

_TIMEOUT_SECONDS = 15.0

# How long "which 山田?" stays an open question. Long enough to think about,
# short enough that an unrelated word an hour later is not mistaken for an
# answer. The robot's own hands-free window is 30s, so this outlasts one
# turn of conversation and not much more.
_PENDING_TTL_SECONDS = 120.0

# Keyed by (device_id, asked_by): the body is the conversation, and a
# different person stepping in front of it has not been asked anything.
# Holds a surname and a deadline -- no rows, because customer records are
# not something to keep warm in case they are wanted again.
_pending: dict[tuple[str, str], dict[str, Any]] = {}

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


# "下の名前は太郎" / "太郎です" / "太郎の方" -- the wrappers a person puts
# around an answer, stripped to leave the answer.
_REFINEMENT_STRIP_RE = re.compile(
    r"^(?:下の)?名前は|^名は|です$|だよ$|の方$|って(人|方)$|さん$|様$|でお願い$")
# Said instead of a name, when the question has stopped mattering.
_CANCEL_RE = re.compile(r"もういい|大丈夫|やめ|キャンセル|なんでもない|いらない")
# An answer to "which one?" is short. Anything longer is a person moving on
# with their day, and moving on is allowed.
_MAX_REFINEMENT_CHARS = 12


def _pending_key(device_id: str, asked_by: str) -> tuple[str, str]:
    return (device_id or "", asked_by or "unknown")


def _remember_question(device_id: str, asked_by: str, surname: str,
                       now: float) -> None:
    _pending[_pending_key(device_id, asked_by)] = {
        "surname": surname, "expires_at": now + _PENDING_TTL_SECONDS}


def _open_question(device_id: str, asked_by: str,
                   now: float) -> Optional[dict[str, Any]]:
    key = _pending_key(device_id, asked_by)
    waiting = _pending.get(key)
    if waiting is None:
        return None
    if now > waiting["expires_at"]:
        _pending.pop(key, None)
        return None
    return waiting


def _forget_question(device_id: str, asked_by: str) -> None:
    _pending.pop(_pending_key(device_id, asked_by), None)


def _as_refinement(text: str) -> Optional[str]:
    """The given name in a reply to "which one?", or None to let it pass.

    While a question is open the bias is to treat a short utterance as its
    answer, because the alternative is worse. Reading "太郎" as
    conversation sends a customer's name to Gemini, which is the one thing
    this module exists to prevent; reading an unrelated word as a name
    costs a puzzled sentence inside a two-minute window that the robot
    itself opened by asking.

    Long utterances and questions are not answers, and fall through.
    """
    stripped = unicodedata.normalize("NFKC", text).strip(" 　。、,.!?！？")
    if not stripped or len(stripped) > _MAX_REFINEMENT_CHARS:
        return None
    if _QUESTION_RE.search(stripped) or "？" in stripped or "?" in stripped:
        return None
    name = _REFINEMENT_STRIP_RE.sub("", stripped).strip()
    name = _REFINEMENT_STRIP_RE.sub("", name).strip()
    return name or None


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
        parts.append("多いから、下の名前を教えて。")
    else:
        parts.append("下の名前を言ってくれたら絞り込むよ。")
    return "".join(parts)


def _lookup(relay_url: str, search: str, asked_by: str, fetch, env
            ) -> tuple[Optional[str], Optional[int], list[dict[str, Any]]]:
    """(error reply, total, rows). The error reply is spoken as-is.

    Every failure produces a sentence rather than a fall-through, because
    falling through hands the customer's name to Gemini. There is
    deliberately no path out of here that does not answer.
    """
    import urllib.parse
    query = urllib.parse.urlencode({"name": search, "asked_by": asked_by or "unknown"})
    try:
        status, payload = fetch(f"{relay_url}/crm/lookup?{query}",
                                env.get("CRM_RELAY_TOKEN", ""))
    except urllib.error.HTTPError as exc:
        _log_status(exc.code)
        if exc.code == 403:
            return "台帳の鍵が合わなかったよ。設定を確認してもらってね。", None, []
        if exc.code in (502, 503):
            return ("会社のシステムが台帳を引けない状態みたい。"
                    "会社のパソコンとCRMを見てもらってね。"), None, []
        return "台帳の照会でエラーが出たよ。", None, []
    except Exception:  # noqa: BLE001 -- unreachable office PC is an expected state
        _log_status(None)
        return ("会社のパソコンに繋がらなくて、台帳が引けないよ。"
                "会社が開いている時間なら、パソコンの電源を確認してね。"), None, []

    _log_status(status, payload.get("count"))
    return None, int(payload.get("count", 0)), (payload.get("results") or [])


def intercept(text: str, asked_by: str,
              fetch: Optional[Callable[[str, str], tuple[int, dict[str, Any]]]] = None,
              env: Optional[dict[str, str]] = None,
              device_id: str = "", now: Optional[float] = None) -> Optional[str]:
    """A spoken reply if this belongs to a grave lookup, else None.

    Two ways in. A full question ("田中さんの墓所どこ？") starts one; a short
    utterance while a "which one?" is outstanding continues it. Both answer
    in every case, including every failure, and neither is remembered.
    """
    env = env if env is not None else os.environ
    relay_url = (env.get("CRM_RELAY_URL") or "").rstrip("/")
    if not relay_url:
        return None
    now = time.time() if now is None else now
    fetch = fetch or _default_fetch

    name = detect(text)
    if name is not None:
        # A fresh question supersedes whatever was being narrowed.
        _forget_question(device_id, asked_by)
        error, total, rows = _lookup(relay_url, name, asked_by, fetch, env)
        if error:
            return error
        if total > 1:
            _remember_question(device_id, asked_by, name, now)
        return _compose_reply(name, total, rows)

    waiting = _open_question(device_id, asked_by, now)
    if waiting is None:
        return None

    if _CANCEL_RE.search(unicodedata.normalize("NFKC", text)):
        _forget_question(device_id, asked_by)
        return "わかった、台帳を見るのはやめておくね。"

    given = _as_refinement(text)
    if given is None:
        return None  # not an answer; ordinary conversation carries on

    surname = waiting["surname"]
    if given == surname:
        return "同じ名字だね。下の名前の方を教えて。"

    full_name = f"{surname} {given}"
    error, total, rows = _lookup(relay_url, full_name, asked_by, fetch, env)
    if error:
        return error
    if total == 0:
        # Keep waiting: a mis-heard given name should cost one more turn,
        # not the whole lookup.
        return f"{surname}さんで{given}という名前は見つからなかったよ。もう一度言ってみて。"
    if total > 1:
        _remember_question(device_id, asked_by, surname, now)
        return _compose_reply(full_name, total, rows)
    _forget_question(device_id, asked_by)
    return _compose_reply(full_name, total, rows)


def _log_status(status: Optional[int], count: Optional[int] = None) -> None:
    # The shape of the traffic, never its subject.
    print(f"[crm_bridge] lookup status={status} count={count}", flush=True)
