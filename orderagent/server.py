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

from . import ai_match, config, congestion, db, intent, stores

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_worker_busy = threading.Lock()  # held while any job is being worked


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
    job["status"] = status
    job["message"] = spoken
    db.upsert_order(job)
    _log(job["job_id"], status, {"message": spoken})
    if job.get("device_id"):
        _announce_async(job["device_id"], spoken)


def _run_job(job_id: str) -> None:
    job = _jobs[job_id]
    if job["chain"] != "mcd":
        _fail(job, "failed", "ごめん、今はマクドナルドだけ対応してるよ。")
        return
    with _worker_busy:
        try:
            _build(job)
        except Exception as exc:  # noqa: BLE001 -- any surprise = stop + tell the human
            _log(job_id, "job_exception", {"error": repr(exc)})
            _fail(job, "failed", "注文の準備中にエラーが起きたよ。詳しくはログを見てね。")


def _build(job: dict[str, Any]) -> None:
    import os
    job["status"] = "building"
    _log(job["job_id"], "building_started")

    # --- store ---------------------------------------------------------
    if job.get("lat") is not None and job.get("lng") is not None:
        candidates = stores.nearest(job["lat"], job["lng"], limit=3)
    else:
        default_key = os.environ.get("ORDER_DEFAULT_STORE_KEY", "45520")  # １０号高鍋店
        candidates = [s for s in stores.all_stores() if s["key"] == default_key]
        if not candidates:
            _fail(job, "failed", "現在地がわからなくて、既定の店舗も見つからなかったよ。")
            return
    store = candidates[0]
    detail = stores.store_detail(store["key"])
    if not detail or not detail.get("mopEnabled"):
        for fallback in candidates[1:]:
            detail = stores.store_detail(fallback["key"])
            if detail and detail.get("mopEnabled"):
                store = fallback
                break
        else:
            _fail(job, "failed", "近くにモバイルオーダー対応の店舗が見つからなかったよ。")
            return
    job["store_key"] = store["key"]
    job["store_name"] = detail.get("name") or store["name"]
    _log(job["job_id"], "store_selected",
         {"key": store["key"], "name": job["store_name"],
          "distance_km": store.get("distance_km")})

    # --- menu match ----------------------------------------------------
    menu_items = stores.menu(store["key"])
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
    from . import mcd_adapter
    try:
        cart = mcd_adapter.build_cart(store["key"], job["items"], job["job_id"],
                                      lambda e, d: _log(job["job_id"], e, d))
    except mcd_adapter.EscalationNeeded as exc:
        _fail(job, "escalated", f"サイト側で人の対応が必要になったよ。{exc}")
        return
    job["cart"] = cart
    job["total_yen"] = cart.get("cart_total_yen")

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
    job["status"] = "awaiting_approval"
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
            job["status"] = "expired"
            db.upsert_order(job)
            _log(job_id, "approval_expired")
            _announce(job["device_id"], "注文の承認が5分なかったから、キャンセルしたよ。")
            return
        time.sleep(2)


def approve_job(job_id: str, phrase: str) -> tuple[int, dict[str, Any]]:
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting_approval":
        return 409, {"error": "not_awaiting_approval"}
    job["status"] = "verifying"
    job["approved"] = dict(job["approved_candidate"], phrase=phrase, approved_at=time.time())
    _log(job_id, "approved_by_voice", {"phrase": phrase})
    db.upsert_order(job)

    from . import payment
    try:
        result = payment.execute(job, job["cart"])
    except payment.PaymentRefused as exc:
        _fail(job, "failed", f"決済前チェックで止めたよ。{exc}")
        return 200, {"status": job["status"]}
    job["payment_executed"] = result.get("payment_executed", False)
    job["status"] = "dry_run_done" if result.get("dry_run") else "paid"
    db.upsert_order(job)
    _log(job_id, "payment_result", result)
    if result.get("dry_run"):
        _announce_async(job["device_id"],
                       f"検証は全部通ったよ。今は練習モードだから決済はしてないよ。"
                       f"合計{result['verified_total_yen']}円、内容は全部確認済み。")
    return 200, {"status": job["status"], "result": result}


def deny_job(job_id: str, phrase: str) -> tuple[int, dict[str, Any]]:
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting_approval":
        return 409, {"error": "not_awaiting_approval"}
    job["status"] = "denied"
    db.upsert_order(job)
    _log(job_id, "denied_by_voice", {"phrase": phrase})
    _announce_async(job["device_id"], "注文をキャンセルしたよ。")
    return 200, {"status": "denied"}


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


def main() -> None:
    config.ensure_dirs()
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"orderagent listening on {config.HOST}:{config.PORT} "
          f"payment_enabled={config.PAYMENT_ENABLED}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
