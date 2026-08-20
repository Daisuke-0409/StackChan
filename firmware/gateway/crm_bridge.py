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
import datetime
import re
import time
import unicodedata
import webbrowser
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


def _remember_ids(device_id: str, asked_by: str, ids: list[int],
                  now: float) -> None:
    """Hold the customer ids the last search produced, and nothing else.

    An id is an integer. The record it names never comes here, so this is
    the one piece of a lookup worth keeping between turns -- it is what
    makes "それモニターに出して" mean something without a second search.
    """
    key = _pending_key(device_id, asked_by)
    waiting = _pending.get(key) or {}
    waiting["ids"] = list(ids)
    waiting["expires_at"] = now + _PENDING_TTL_SECONDS
    _pending[key] = waiting


def _remember_find(device_id: str, asked_by: str, conditions: dict[str, str],
                   now: float) -> None:
    """Hold the question, not the answers.

    Same reasoning as the surname wait: a refinement re-queries rather than
    reading from rows kept warm, so the CRM's audit log sees the narrower
    question too, and no customer record lives here between turns.
    """
    _pending[_pending_key(device_id, asked_by)] = {
        "conditions": dict(conditions), "expires_at": now + _PENDING_TTL_SECONDS}


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


# --- searching by circumstance (find) --------------------------------------
#
# "最近受けた案件で加江田のお客さんで俺の担当の人いなかったっけ" -- three
# conditions and a question, none of which is a name. Recognised on the same
# terms as a grave question: several signals together, because an
# interceptor this close to customer data must not fire on small talk.

_CUSTOMER_RE = re.compile(r"お客(さん|様)?|顧客|得意先")
_PHONE_WANTED_RE = re.compile(r"電話|連絡先|番号")
_FIND_QUESTION_RE = re.compile(r"いない|いなかった|いる|いた|誰|だれ|教えて|調べて|"
                               r"どの|どんな|ある|あった")

# "俺の担当" resolves to whoever is speaking; a named one to that name.
_MINE_RE = re.compile(r"の?(?:俺|私|僕|わたし|おれ|ぼく|自分)の担当")
_NAMED_STAFF_RE = re.compile(r"の?([^\s。、,，！？!?の]{1,8})(?:さん)?の担当")
# The honorific is optional in the pattern, so a greedy name swallows it
# and 篠崎さん arrives where 篠崎 was meant. Trim it after the fact rather
# than making the pattern harder to read.
_HONORIFIC_RE = re.compile(r"(?:さん|様|くん|ちゃん)$")

# Periods a person actually says. "最近" has no business meaning -- the CRM
# said so when asked -- so it is turned into a date here rather than left
# for them to guess at.
_RECENT_DAYS = 90
_PERIOD_RE = (
    ("recent", re.compile(r"最近|このごろ|この頃")),
    ("this_month", re.compile(r"今月")),
    ("last_month", re.compile(r"先月")),
    ("this_year", re.compile(r"今年")),
)

# The district is whatever is left saying "◯◯のお客さん" once the staff and
# period phrases are out of the way. Stripping them first matters: without
# it, "俺の担当のお客さん" makes 担当 look like a place.
_AREA_RE = re.compile(r"([^\s。、,，！？!?のでを]{2,10})(?:地区|の)(?:お客|顧客|得意先|方)")

# Roles allowed to hear a phone number read out loud. Deliberately just the
# one. A colleague standing at the office robot can look the number up in
# the CRM, where the screen shows it to them and nobody else; the robot
# says it to the whole room. Widening this is one line, and should be a
# decision rather than a drift.
_PHONE_ROLES = ("master",)

# "田中さんの電話番号教えて" has no district, staff or period, so without
# this it would fall through to ordinary chat and hand the name to Gemini.
# Here the name is the condition.
_NAMED_PHONE_RE = re.compile(
    r"([^\s。、,，！？!?の]{1,12})(?:さん|様)の(?:電話|連絡先|番号)")

# "お客さんの電話番号" is not a request about somebody called お客. Words
# that are roles rather than names have to be excluded by hand, because
# nothing in the shape of the sentence tells them apart -- found by asking
# the live CRM and watching it search for a customer named 客.
_NOT_A_PERSON = ("お客", "客", "顧客", "得意先", "担当", "うち", "自分",
                 "俺", "私", "僕", "わたし", "おれ", "ぼく", "その人", "あの人")


def _since_from(text: str, today: Optional[datetime.date] = None) -> Optional[str]:
    """A date string the CRM understands, from the way people say time."""
    today = today or datetime.date.today()
    for kind, pattern in _PERIOD_RE:
        if not pattern.search(text):
            continue
        if kind == "recent":
            return (today - datetime.timedelta(days=_RECENT_DAYS)).isoformat()
        if kind == "this_month":
            return today.replace(day=1).isoformat()
        if kind == "last_month":
            first = today.replace(day=1)
            return (first - datetime.timedelta(days=1)).replace(day=1).isoformat()
        if kind == "this_year":
            return today.replace(month=1, day=1).isoformat()
    return None


def detect_find(text: str, asked_by: str,
                today: Optional[datetime.date] = None) -> Optional[dict[str, str]]:
    """Conditions for a circumstance search, or None to leave it as chat.

    Requires a customer word, a question, and at least one condition. Any
    two of those show up in ordinary conversation; all three together do
    not.
    """
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    if not _CUSTOMER_RE.search(normalized) and not _PHONE_WANTED_RE.search(normalized):
        return None
    if not _FIND_QUESTION_RE.search(normalized) and not _PHONE_WANTED_RE.search(normalized):
        return None

    conditions: dict[str, str] = {}
    remainder = normalized

    if _MINE_RE.search(remainder):
        conditions["staff"] = asked_by or ""
        remainder = _MINE_RE.sub("", remainder)
    else:
        named = _NAMED_STAFF_RE.search(remainder)
        if named:
            staff = _HONORIFIC_RE.sub("", named.group(1)).strip()
            if staff and staff not in _NOT_A_PERSON:
                conditions["staff"] = staff
                remainder = remainder.replace(named.group(0), "")

    since = _since_from(remainder, today)
    if since:
        conditions["since"] = since
        for _, pattern in _PERIOD_RE:
            remainder = pattern.sub(" ", remainder)

    area = _AREA_RE.search(remainder)
    if area:
        conditions["area"] = area.group(1)

    named = _NAMED_PHONE_RE.search(normalized)
    if named:
        who = _HONORIFIC_RE.sub("", named.group(1)).strip()
        if who and who not in _NOT_A_PERSON:
            conditions["name"] = who

    if not any(conditions.get(key) for key in ("area", "staff", "since", "name")):
        return None
    return {key: value for key, value in conditions.items() if value}


def _spoken_conditions(conditions: dict[str, str]) -> str:
    parts = []
    if conditions.get("area"):
        parts.append(f"{conditions['area']}の")
    if conditions.get("staff"):
        parts.append(f"{conditions['staff']}さん担当の")
    if conditions.get("since"):
        parts.append("最近の")
    return "".join(parts) or "その条件の"


def _compose_find_reply(conditions: dict[str, str], total: int,
                        rows: list[dict[str, Any]], may_hear_phone: bool) -> str:
    where = _spoken_conditions(conditions)
    if total == 0:
        return f"{where}お客さんは見つからなかったよ。"

    if total == 1 and rows:
        row = rows[0]
        name = (row.get("customer_name") or "").strip()
        if not may_hear_phone:
            return (f"{where}お客さんは{name}さんだよ。"
                    "電話番号は声では言わないから、CRMの画面で見てね。")
        phone = (row.get("phone") or "").strip()
        if not phone:
            return f"{where}お客さんは{name}さんだよ。電話番号は台帳に入っていなかった。"
        return f"{where}お客さんは{name}さんだよ。電話番号は{phone}。"

    names = "、".join((row.get("customer_name") or "").strip() for row in rows)
    parts = [f"{where}お客さんは{total}人いるよ。{names}。"]
    if total > len(rows):
        parts.append("多いから、名前で絞ってね。")
    else:
        parts.append("誰の電話番号？")
    return "".join(parts)


# --- putting a record on a screen (show) -----------------------------------

_SHOW_RE = re.compile(r"(モニター|画面|ディスプレイ|そっち|そこ)に?\s*(出して|映して|表示)|"
                      r"表示して|映して|出しといて")

# Which bodies stand next to the office monitor. Anything not listed opens
# on whichever machine the gateway is running on, because the body being
# spoken to is where the person is, and an unlisted one is more likely to
# be at home than in the office.
def _office_devices(env: dict[str, str]) -> set[str]:
    return {d.strip().upper() for d in (env.get("CRM_OFFICE_DEVICE_IDS") or "").split(",")
            if d.strip()}


def detect_show(text: str) -> bool:
    """Whether this asks for the last customer to be put on a screen."""
    if not text:
        return False
    return bool(_SHOW_RE.search(unicodedata.normalize("NFKC", text)))


def _ids_of(rows: list[dict[str, Any]]) -> list[int]:
    out = []
    for row in rows:
        value = row.get("customer_id")
        if value is not None and str(value).isdigit():
            out.append(int(value))
    return out


def _show_on_screen(relay_url: str, customer_id: int, device_id: str,
                    asked_by: str, fetch, env: dict[str, str]) -> str:
    """Put one record on the screen where the person is standing.

    Which screen is decided by which body is being spoken to. That is the
    only signal that does not need guessing: the robot someone is talking
    to is in the room with them.

    The office monitor is driven by the relay, which is the machine next to
    it. Anywhere else opens on whichever machine the gateway runs on. In
    both cases the browser fetches the record itself -- nothing about the
    customer passes through the robot -- and in both cases the CRM still
    asks for a login, which is the CRM's decision and the right one for a
    page that shows an address, a phone number and every photo.
    """
    import urllib.parse

    if device_id.upper() in _office_devices(env):
        query = urllib.parse.urlencode({"customer_id": customer_id,
                                        "asked_by": asked_by or "unknown"})
        try:
            fetch(f"{relay_url}/crm/open?{query}", env.get("CRM_RELAY_TOKEN", ""))
        except Exception:  # noqa: BLE001
            _log_status(None)
            return "会社の画面に出せなかったよ。会社のパソコンを確認してね。"
        return "会社のモニターに出したよ。"

    # Somewhere else: ask for a link that works from here, and open it here.
    office_host = urllib.parse.urlsplit(relay_url).hostname or ""
    query = urllib.parse.urlencode({"customer_id": customer_id,
                                    "asked_by": asked_by or "unknown",
                                    "host": f"{office_host}:8765"})
    try:
        _, payload = fetch(f"{relay_url}/crm/show?{query}",
                           env.get("CRM_RELAY_TOKEN", ""))
    except Exception:  # noqa: BLE001
        _log_status(None)
        return "台帳の画面を開けなかったよ。会社のパソコンを確認してね。"
    url = (payload or {}).get("url")
    if not url:
        return "台帳の画面のアドレスが分からなかったよ。"
    webbrowser.open(url)
    return "こっちの画面に出したよ。CRMのログインを聞かれたら入れてね。"


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


def _find(relay_url: str, conditions: dict[str, str], asked_by: str, fetch, env
          ) -> tuple[Optional[str], Optional[int], list[dict[str, Any]]]:
    """(error reply, total, rows) for a circumstance search."""
    import urllib.parse
    query = urllib.parse.urlencode(dict(conditions, asked_by=asked_by or "unknown"))
    try:
        status, payload = fetch(f"{relay_url}/crm/find?{query}",
                                env.get("CRM_RELAY_TOKEN", ""))
    except urllib.error.HTTPError as exc:
        _log_status(exc.code)
        if exc.code == 403:
            return "台帳の鍵が合わなかったよ。設定を確認してもらってね。", None, []
        if exc.code == 400:
            return "条件がうまく取れなかったよ。地区か担当か時期を教えて。", None, []
        return ("会社のシステムが台帳を引けない状態みたい。"
                "会社のパソコンとCRMを見てもらってね。"), None, []
    except Exception:  # noqa: BLE001
        _log_status(None)
        return ("会社のパソコンに繋がらなくて、台帳が引けないよ。"
                "会社が開いている時間なら、パソコンの電源を確認してね。"), None, []

    _log_status(status, payload.get("count"))
    return None, int(payload.get("count", 0)), (payload.get("results") or [])


def intercept(text: str, asked_by: str,
              fetch: Optional[Callable[[str, str], tuple[int, dict[str, Any]]]] = None,
              env: Optional[dict[str, str]] = None,
              device_id: str = "", now: Optional[float] = None,
              role: str = "unknown") -> Optional[str]:
    """A spoken reply if this belongs to a CRM lookup, else None.

    Three ways in: a grave question, a search by circumstance, and a short
    utterance while either has an outstanding "which one?". All of them
    answer in every case, including every failure, and none is remembered.

    `role` decides whether a phone number may be spoken. Everything else is
    the same for everyone: where a grave is, is not a secret from a
    colleague standing at the office robot.
    """
    env = env if env is not None else os.environ
    relay_url = (env.get("CRM_RELAY_URL") or "").rstrip("/")
    if not relay_url:
        return None
    now = time.time() if now is None else now
    fetch = fetch or _default_fetch

    if detect_show(text):
        waiting = _open_question(device_id, asked_by, now) or {}
        ids = waiting.get("ids") or []
        if not ids:
            return "先に誰のことか探させて。名前か地区を言ってくれれば台帳を見るよ。"
        if len(ids) > 1:
            return "誰を出す？名前で教えて。"
        return _show_on_screen(relay_url, ids[0], device_id, asked_by, fetch, env)

    conditions = detect_find(text, asked_by)
    if conditions is not None:
        _forget_question(device_id, asked_by)
        error, total, rows = _find(relay_url, conditions, asked_by, fetch, env)
        if error:
            return error
        if total > 1:
            _remember_find(device_id, asked_by, conditions, now)
        _remember_ids(device_id, asked_by, _ids_of(rows), now)
        return _compose_find_reply(conditions, total, rows, role in _PHONE_ROLES)

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

    if "conditions" in waiting:
        # Narrowing a circumstance search: add the name to the same
        # conditions and ask again, rather than reading from rows nobody
        # kept.
        narrowed = dict(waiting["conditions"], name=given)
        error, total, rows = _find(relay_url, narrowed, asked_by, fetch, env)
        if error:
            return error
        if total > 1:
            _remember_find(device_id, asked_by, waiting["conditions"], now)
        else:
            _forget_question(device_id, asked_by)
        _remember_ids(device_id, asked_by, _ids_of(rows), now)
        return _compose_find_reply(narrowed, total, rows, role in _PHONE_ROLES)

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
