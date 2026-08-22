"""Order agent HTTP server -- the 母艦 side of mobile ordering.

Runs on 127.0.0.1:8766, deliberately loopback-only: its callers are the
gateway's order bridge on the same box, nothing else. One job at a time
(注意点3), each job a thread, every transition audited to sqlite.

Lifecycle:
  created -> building -> awaiting_approval -> verifying -> dry_run_done|paid
                    \-> needs_info | failed | escalated | denied | expired
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from . import (ai_match, config, congestion, db, intent, reconcile,
               states, stores)
from . import snapshot as snapshot_mod

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_worker_busy = threading.Lock()  # held while any job is being worked


def _set_status(job: dict[str, Any], target: str, *, force: bool = False) -> None:
    """Move a job through the state machine, or refuse (B1 wiring).

    Every status change funnels through states.advance, so an edge the
    machine does not have -- an approval heard twice, a payment retried --
    raises instead of happening. `force` exists for one caller: _fail,
    which is how a human gets told about a job that is already off the
    rails, and must not itself die on the way. A forced move is logged as
    the anomaly it is.
    """
    try:
        job["status"] = states.advance(job["status"], target)
    except states.IllegalTransition:
        if not force:
            raise
        _log(job["job_id"], "illegal_transition_forced",
             {"from": job["status"], "to": target})
        job["status"] = target


def _log(job_id: str, event: str, detail: Optional[dict] = None) -> None:
    print(f"orderagent {event} job={job_id} {json.dumps(detail or {}, ensure_ascii=False)[:400]}",
          flush=True)
    db.record(job_id, event, detail)


def _announce_async(device_id: str, text: str) -> None:
    """_announce without blocking the caller -- HTTP handlers use this so
    a multi-sentence TTS (seconds) never pushes their response past the
    order bridge's timeout (observed as a false 繋がらなかった)."""
    threading.Thread(target=_announce, args=(device_id, text), daemon=True).start()


def _announce(device_id: str, text: str) -> None:
    """Says `text` out loud through the gateway (and thus the robot)."""
    import os
    token = os.environ.get("DEVICE_TOKEN", "")
    body = json.dumps({"device_id": device_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{config.GATEWAY_URL}/v1/announce", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(request, timeout=30)
    except Exception as exc:  # an unheard announcement must not kill the job
        print(f"orderagent announce_failed: {exc}", flush=True)


def submit_job(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /jobs -- starts a job unless one is already in flight."""
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] in ("created", "building", "awaiting_approval", "verifying"):
                return 409, {"error": "busy",
                             "message": "別の注文が進行中です。先にそちらを完了/キャンセルしてください。"}
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "created_at": time.time(),
            "status": "created",
            "chain": payload.get("chain", "mcd"),
            "device_id": payload.get("device_id", ""),
            "item_text": payload.get("item_text", ""),
            "quantity": int(payload.get("quantity", 1)),
            "pickup": payload.get("pickup"),
            "lat": payload.get("lat"),
            "lng": payload.get("lng"),
        }
        _jobs[job_id] = job
    db.upsert_order(job)
    _log(job_id, "job_created", {k: job[k] for k in ("chain", "item_text", "quantity", "pickup")})
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return 200, {"job_id": job_id}


def _fail(job: dict[str, Any], status: str, spoken: str) -> None:
    _set_status(job, status, force=True)
    job["message"] = spoken
    db.upsert_order(job)
    _log(job["job_id"], status, {"message": spoken})
    if job.get("device_id"):
        _announce_async(job["device_id"], spoken)


def _adapter_for(chain: str):
    """The adapter for a chain, or None when nothing can serve it.

    Imports are deferred: the Starbucks adapter pulls in the browser layer,
    and a McDonald's order should not pay for that.
    """
    if chain == "mcd":
        from .mcd import McdAdapter
        return McdAdapter()
    if chain == "starbucks":
        from .starbucks import StarbucksAdapter
        return StarbucksAdapter()
    if chain == "mos":
        from .mos import MosAdapter
        return MosAdapter()
    return None


CHAIN_NAMES = {"mcd": "マクドナルド", "starbucks": "スターバックス",
               "mos": "モスバーガー", "kfc": "ケンタッキー"}


def _run_job(job_id: str) -> None:
    job = _jobs[job_id]
    adapter = _adapter_for(job["chain"])
    if adapter is None:
        spoken = CHAIN_NAMES.get(job["chain"], job["chain"])
        _fail(job, "failed",
              f"ごめん、{spoken}はまだ対応してないよ。今はマクドナルドとスタバだけ。")
        return
    with _worker_busy:
        try:
            _build(job, adapter)
        except Exception as exc:  # noqa: BLE001 -- any surprise = stop + tell the human
            _log(job_id, "job_exception", {"error": repr(exc)})
            _fail(job, "failed", _explain(exc))


def _explain(exc: Exception) -> str:
    """A sentence the robot can say for a failure it did not expect.

    Chain-specific conditions carry their own wording -- an expired login
    and a leftover basket need different actions from Daisuke, and
    "エラーが起きたよ" tells him neither.
    """
    name = type(exc).__name__
    if name in ("SessionExpired", "LeftoverCart", "EscalationNeeded", "NotYetWalked"):
        return str(exc)
    return "注文の準備中にエラーが起きたよ。詳しくはログを見てね。"


def _build(job: dict[str, Any], adapter: Any) -> None:
    import os
    _set_status(job, states.BUILDING)
    _log(job["job_id"], "building_started")

    # --- store ---------------------------------------------------------
    # No phone position means the house: ordering is still useful without a
    # location, and a default is honest in a way that guessing is not.
    lat = job.get("lat")
    lng = job.get("lng")
    if lat is None or lng is None:
        lat = float(os.environ.get("ORDER_DEFAULT_LAT", "32.1337"))   # 高鍋町
        lng = float(os.environ.get("ORDER_DEFAULT_LNG", "131.5033"))
        _log(job["job_id"], "location_defaulted", {"lat": lat, "lng": lng})

    candidates = adapter.find_stores(lat, lng, limit=4)
    if not candidates:
        _fail(job, "failed", "近くにお店が見つからなかったよ。")
        return
    store = next((s for s in candidates
                  if adapter.capabilities(s["id"]).online_order), None)
    if store is None:
        # Every nearby branch is shut or cannot take a web order. Which one
        # it is matters to the person waiting, so say the nearest by name.
        nearest = candidates[0]
        _fail(job, "failed",
              f"近くのお店は今どこも注文を受け付けてないみたい。"
              f"いちばん近いのは{nearest['name']}だよ。")
        return
    job["store_key"] = store["id"]
    job["store_name"] = store["name"]
    _log(job["job_id"], "store_selected",
         {"id": store["id"], "name": store["name"],
          "distance_km": store.get("distance_km")})

    # --- menu match ----------------------------------------------------
    menu_items = adapter.menu(store["id"])
    if not menu_items:
        _fail(job, "failed", "メニューが取得できなかったよ。")
        return
    parts = intent.split_items(job["item_text"])
    resolved = []
    interpretations = []  # spoken -> resolved name, said aloud in the readback
    for part in parts:
        matches = intent.match_menu(part, menu_items)
        if not matches:
            # 通称・略称 (ダブチ, シャカポテ...) は正式名の部分文字列ですら
            # ないので、AIにメニューを読ませて推測させる。推測は読み上げで
            # 必ず「〜と解釈したよ」と提示され、承認ゲートを通る。
            matches = ai_match.suggest(part, menu_items)
            if not matches:
                _fail(job, "needs_info",
                      f"「{part}」がメニューに見つからなかったよ。別の言い方でもう一度お願い。")
                return
            interpretations.append(f"「{part}」は{matches[0]['name']}のことだと解釈したよ。")
            _log(job["job_id"], "ai_menu_guess",
                 {"spoken": part, "guessed": matches[0]["name"]})
        resolved.append(matches[0])
    # A spoken quantity applies to a single-item order; multi-item orders
    # take one of each (per-item counts can come later -- guessing which
    # item a number belonged to is not acceptable this close to a payment).
    per_item_quantity = job["quantity"] if len(resolved) == 1 else 1
    job["items"] = [{"id": p["id"], "name": p["name"], "price": p["price"],
                     "quantity": per_item_quantity} for p in resolved]

    # --- congestion (FR-3: estimate only, and says so in the log) ------
    estimate = congestion.estimate()
    job["congestion"] = estimate
    _log(job["job_id"], "congestion_estimated", estimate)

    # --- cart ----------------------------------------------------------
    # The fulfillment travels with the items: Starbucks picks it before the
    # menu is even shown, so it cannot wait until the cart is read back.
    cart_items = [dict(item, fulfillment=job.get("pickup") or "takeout")
                  for item in job["items"]]
    try:
        cart = adapter.build_cart(store["id"], cart_items, job["job_id"],
                                  lambda e, d: _log(job["job_id"], e, d))
    except Exception as exc:  # noqa: BLE001 -- each chain names its own conditions
        if type(exc).__name__ == "EscalationNeeded":
            _fail(job, "escalated", f"サイト側で人の対応が必要になったよ。{exc}")
            return
        raise
    job["cart"] = cart
    job["total_yen"] = cart.get("cart_total_yen")

    # Prepaid chains say up front whether the card covers the bill. Asking
    # for approval on an order that cannot be paid for wastes the one thing
    # the approval is for, so stop here and say what would fix it.
    if cart.get("sufficient_balance") is False:
        balance = cart.get("balance_yen")
        _fail(job, "needs_info",
              f"カードの残高が足りないよ。"
              f"{f'今の残高は{balance}円で、' if balance is not None else ''}"
              f"合計は{cart.get('cart_total_yen')}円。入金してからもう一度言ってね。")
        return

    # --- approval request (FR-5) --------------------------------------
    site_total = job["total_yen"]
    if site_total is None:
        _fail(job, "failed", "サイト側の金額が読み取れなかったから中止したよ。")
        return
    job["approved_candidate"] = {
        "expected_total_yen": site_total,
        "expected_item_count": len(cart.get("cart_items") or []),
        "expected_items": [{"name": line["name"], "price": line["price"],
                            "quantity": line.get("quantity", 1)}
                           for line in cart.get("cart_items") or []],
    }
    # Receive method honesty: 高鍋店 has no drive-through pickup on the web
    # order (store-dependent), so a drive-through request falls back to
    # takeout and the readback SAYS so -- silent substitutions and payment
    # approvals don't mix.
    pickup_note = ""
    options = cart.get("pickup_options") or []
    if job.get("pickup") == "drive_through" and "drive_through" not in options:
        job["pickup"] = "takeout"
        pickup_note = "この店はドライブスルー受け取りが選べないから、お持ち帰りにするね。"
    pickup_spoken = {"drive_through": "ドライブスルー受け取り", "takeout": "お持ち帰り",
                     "eatin": "店内", None: "受け取り方法未指定"}[job.get("pickup")]
    advice = pickup_note
    if job.get("pickup") == "drive_through" and estimate["level"] == "busy":
        advice += estimate["advice"]
    spoken_total = site_total
    # Freeze what is about to be read aloud (再設計 §22): the approval that
    # may follow consents to THIS -- this store, these lines, this total --
    # and reconciliation later compares the store's history against it.
    job["snapshot"] = snapshot_mod.Snapshot(
        store_id=str(store["id"]), store_name=store["name"],
        fulfillment=job.get("pickup"),
        lines=[{"name": line["name"], "price": int(line["price"]),
                "quantity": int(line.get("quantity", 1))}
               for line in cart.get("cart_items") or []],
        total_yen=site_total).to_dict()
    _set_status(job, states.AWAITING_APPROVAL)
    job["approval_expires_at"] = time.time() + config.APPROVAL_TIMEOUT_SECONDS
    db.upsert_order(job)
    # The readback describes the CART as scraped, never the request: what
    # gets approved must be what the site will charge for. Duplicate rows
    # of the same line are aggregated for speech only.
    counted: dict[tuple[str, int], int] = {}
    for line in cart.get("cart_items") or []:
        key = (line["name"], line["price"])
        counted[key] = counted.get(key, 0) + line.get("quantity", 1)
    items_spoken = "、".join(f"{name} {qty}点" for (name, _), qty in counted.items())
    interpretation_note = "".join(interpretations)
    summary = (f"{interpretation_note}{job['store_name']}、{items_spoken}、"
               f"{pickup_spoken}、合計{spoken_total}円。{advice}"
               f"注文は決済後キャンセルできないよ。注文していい？"
               f"「注文して」で確定、「キャンセル」で中止だよ。")
    job["approval_summary"] = summary
    _log(job["job_id"], "awaiting_approval", {"summary": summary})
    _announce(job["device_id"], summary)
    threading.Thread(target=_expire_watch, args=(job["job_id"],), daemon=True).start()


def _expire_watch(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    while job["status"] == "awaiting_approval":
        if time.time() > job["approval_expires_at"]:
            # Claim under the lock, like approve_job does: between this
            # thread noticing the deadline and acting on it, an approval
            # may have claimed the job and be mid-payment. Overwriting
            # "verifying" with "expired" would tell the user their order
            # was cancelled while the pay button is being pressed.
            with _jobs_lock:
                if job["status"] != "awaiting_approval":
                    return
                _set_status(job, states.EXPIRED)
            db.upsert_order(job)
            _log(job_id, "approval_expired")
            _announce(job["device_id"], "注文の承認が5分なかったから、キャンセルしたよ。")
            return
        time.sleep(2)


def approve_job(job_id: str, phrase: str) -> tuple[int, dict[str, Any]]:
    # Claim the job atomically: read the status and move it to "verifying"
    # inside one critical section, so a retried approve that arrives while
    # payment.execute() is still clicking (it blocks 15-20s) cannot pass the
    # status check a second time and drive a second payment. This is the
    # "double approve" the payment gate's own docstring forbids.
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not states.accepts_approval(job["status"]):
            return 409, {"error": "not_awaiting_approval"}
        _set_status(job, states.VERIFYING)
        job["approved"] = dict(job["approved_candidate"], phrase=phrase,
                               approved_at=time.time())
    _log(job_id, "approved_by_voice", {"phrase": phrase})
    db.upsert_order(job)

    from . import payment
    job["payment_attempted_at"] = time.time()
    try:
        result = payment.execute(job, job["cart"])
    except payment.PaymentRefused as exc:
        _fail(job, "failed", f"決済前チェックで止めたよ。{exc}")
        return 200, {"status": job["status"]}
    job["payment_executed"] = result.get("payment_executed", False)
    job["order_number"] = result.get("order_number")
    _log(job_id, "payment_result", result)

    if result.get("dry_run"):
        _set_status(job, states.DRY_RUN_DONE)
        spoken = (f"検証は全部通ったよ。今は練習モードだから決済はしてないよ。"
                  f"合計{result['verified_total_yen']}円、内容は全部確認済み。")
    else:
        outcome = result.get("outcome")
        if outcome == "PAID":
            _set_status(job, states.PAID)
            spoken = (f"注文できたよ。注文番号は{result['order_number']}、"
                      f"{job['store_name']}で受け取ってね。"
                      f"合計{result['verified_total_yen']}円だったよ。")
        elif outcome == "NOT_PLACED":
            _set_status(job, states.FAILED)
            spoken = "決済のボタンが効かなかったよ。注文は入っていないはず。もう一度言ってね。"
        else:
            # UNKNOWN: charged or not, nobody here can tell. Never retried
            # (§26/§27) -- but before giving up on knowing, ask the store
            # what it thinks happened. reconcile may settle it either way;
            # what it cannot settle goes to a person.
            _set_status(job, states.PAYMENT_UNCERTAIN)
            db.upsert_order(job)
            spoken = _reconcile_uncertain(job) or (
                result.get("note")
                or "決済の結果が確認できなかったよ。注文履歴を見てね。")
    db.upsert_order(job)
    _announce_async(job["device_id"], spoken)
    return 200, {"status": job["status"], "result": result}


def deny_job(job_id: str, phrase: str) -> tuple[int, dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not states.accepts_approval(job["status"]):
            return 409, {"error": "not_awaiting_approval"}
        _set_status(job, states.DENIED)
    db.upsert_order(job)
    _log(job_id, "denied_by_voice", {"phrase": phrase})
    _announce_async(job["device_id"], "注文をキャンセルしたよ。")
    return 200, {"status": "denied"}


def _reconcile_uncertain(job: dict[str, Any]) -> Optional[str]:
    """A payment with no answer: look at the store, never retry (§27).

    Returns what to say, or None when reconciliation had nothing to add.
    Today every adapter answers recent_orders with history_available=False,
    so this settles nothing yet -- but the path is wired, the verdict is
    logged, and the day an adapter learns to read its history, uncertain
    payments start resolving themselves with no further change here.
    """
    adapter = _adapter_for(job["chain"])
    raw = job.get("snapshot")
    if adapter is None or not raw:
        return None
    approved = snapshot_mod.Snapshot.from_dict(raw)
    try:
        evidence = adapter.recent_orders(job.get("store_key") or "")
    except Exception as exc:  # noqa: BLE001 -- failing to look is UNRESOLVED, not a crash
        evidence = reconcile.Evidence(
            history_available=False,
            note=f"注文履歴の読み取りにも失敗したよ。")
        _log(job["job_id"], "reconcile_evidence_failed", {"error": repr(exc)})
    verdict = reconcile.reconcile(
        approved, evidence,
        job.get("payment_attempted_at") or job.get("created_at") or time.time())
    job["reconcile"] = verdict.to_dict()
    _log(job["job_id"], "reconciled", verdict.to_dict())
    if verdict.is_confirmed():
        job["order_number"] = verdict.order_number
        _set_status(job, states.PAID)
    elif verdict.outcome == reconcile.FAILED:
        _set_status(job, states.FAILED)
    return reconcile.spoken(verdict)


def _sweep_interrupted() -> None:
    """What the last process left behind, settled before taking new work (B3).

    Without this, a crash mid-job leaves the database saying "verifying"
    forever while the restarted agent answers 404 -- a job that may have
    charged money, findable by nobody. Three kinds of leftovers:

    - verifying: the frightening one. The process died somewhere around the
      pay click and nobody read the answer. It becomes payment_uncertain,
      goes through reconciliation like any other uncertain payment, and is
      announced so a person knows to look.
    - payment_uncertain: already waiting for a person; reload it so
      /jobs/<id> answers and the reminder stands.
    - everything else active: conversations and carts died with the
      browser. Marked failed, announced only if someone was mid-approval.
    """
    unfinished = db.load_unfinished(sorted(states.ACTIVE) + [states.PAYMENT_UNCERTAIN])
    for job in unfinished:
        with _jobs_lock:
            _jobs[job["job_id"]] = job
        job["restored"] = True
        if job["status"] == states.VERIFYING:
            _set_status(job, states.PAYMENT_UNCERTAIN)
            _log(job["job_id"], "restored_verifying_as_uncertain")
            spoken = _reconcile_uncertain(job) or states.describe(job["status"])
            db.upsert_order(job)
            if job.get("device_id"):
                _announce_async(job["device_id"],
                                f"再起動する前に決済していた注文があるよ。{spoken}")
        elif job["status"] == states.PAYMENT_UNCERTAIN:
            _log(job["job_id"], "restored_uncertain")
        else:
            was = job["status"]
            announce = (was == states.AWAITING_APPROVAL) and job.get("device_id")
            _set_status(job, states.FAILED, force=True)
            job["message"] = "エージェントが再起動して、途中だった注文は取り消したよ。"
            db.upsert_order(job)
            _log(job["job_id"], "restored_as_failed", {"was": was})
            if announce:
                _announce_async(job["device_id"],
                                "さっきの注文は、こっちの再起動で取り消しちゃったよ。"
                                "もう一度言ってね。")


def pending_approval() -> Optional[dict[str, Any]]:
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "awaiting_approval":
                return {"job_id": job["job_id"], "summary": job["approval_summary"],
                        "expires_at": job["approval_expires_at"]}
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Optional[dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                return None
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "payment_enabled": config.PAYMENT_ENABLED})
        elif parsed.path == "/pending":
            self._send(200, {"pending": pending_approval()})
        elif parsed.path == "/stores":
            query = parse_qs(parsed.query)
            try:
                lat = float(query["lat"][0])
                lng = float(query["lng"][0])
            except (KeyError, ValueError):
                self._send(400, {"error": "invalid_input"})
                return
            found = stores.nearest(lat, lng, limit=3)
            self._send(200, {"stores": [{"key": s["key"], "name": s["name"],
                                         "address": s["address"],
                                         "distance_km": s["distance_km"]} for s in found]})
        elif parsed.path.startswith("/jobs/"):
            job = _jobs.get(parsed.path.split("/")[2])
            if not job:
                self._send(404, {"error": "not_found"})
                return
            public = {k: v for k, v in job.items() if k not in ("cart",)}
            self._send(200, public)
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parts = self.path.strip("/").split("/")
        payload = self._read_json()
        if payload is None:
            self._send(400, {"error": "invalid_input"})
            return
        if parts == ["jobs"]:
            self._send(*submit_job(payload))
        elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "approve":
            self._send(*approve_job(parts[1], payload.get("phrase", "")))
        elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "deny":
            self._send(*deny_job(parts[1], payload.get("phrase", "")))
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # request lines go through _log instead


class _SingleInstanceServer(ThreadingHTTPServer):
    """Refuses to start when the port is already taken.

    Python turns SO_REUSEADDR on by default, and on Windows that does not
    mean what it means elsewhere: a second process can bind a port another
    is already listening on, and requests are split between them at random.
    The visible symptom is that a change does not take effect, because the
    old process is still answering half the time.

    It matters more here than anywhere else in this repo. A stale agent
    holds its own job table, its own approval state and its own reading of
    ORDER_PAYMENT_ENABLED, so an approval spoken to one instance can arrive
    at another that never read the order aloud. The one failure this whole
    process is built to avoid is paying twice, and two of it is the
    shortest path there.

    The CRM hit this on 2026-08-19 and the CRM relay on 2026-08-20; this is
    the same fix, applied before it bites.
    """

    allow_reuse_address = False


def main() -> None:
    config.ensure_dirs()
    _sweep_interrupted()
    try:
        server = _SingleInstanceServer((config.HOST, config.PORT), Handler)
    except OSError as exc:
        # Naming the likely cause beats an errno: the usual reason is that
        # the logon task already started one.
        raise SystemExit(
            f"orderagent could not take {config.HOST}:{config.PORT} ({exc}). "
            "Another one is probably already running -- check the "
            "'Tachikoma Order Agent' task before starting a second."
        ) from exc
    print(f"orderagent listening on {config.HOST}:{config.PORT} "
          f"payment_enabled={config.PAYMENT_ENABLED}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
