"""R6 の受け入れ条件を、動いている本物の CRM に対して確かめる。

これは単体テストではない。**会社PCでしか通らない**し、実際の台帳を引く。
`run_tests.ps1` には入れていない: どこでも走るべきものではないため。

    powershell -ExecutionPolicy Bypass -File firmware\\gateway\\run_crm_relay.ps1  # 動いていること
    python firmware/gateway/check_crm_live.py

**顧客の値は一度も表示しない。**件数と、返ってきた項目の名前と、
組み立てた文が満たすべき性質だけを見る。R6 が問うているのは「何が返るか」
であって「誰が返るか」ではないので、それで足りる。

**照会1回につき CRM の監査ログに1行残る**(追記のみ・削除APIなし)。だから
`asked_by` には人の名前ではなく acceptance-check を渡す。あとから台帳を見た人が
「これは点検だ」と分かるように。契約上、登録済み担当者名に一致しない値は
拒否されずに unknown 相当として通る。

いつまた要るか: CRM を NAS へ移したとき。あのとき変わるのは接続先ホストだけの
はずで、それを確かめる手段がこれ。
"""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.environ.get("CRM_RELAY_URL", "http://127.0.0.1:8767")
LOG = os.environ.get("TACHIKOMA_LOG_FILE", "")

# 照会役が声に出してよいとしている3項目 (crm_relay.ALLOWED_FIELDS と同じ)。
ALLOWED = {"customer_name", "cemetery_name", "area"}

# 返ってきたら設計が壊れているもの。zenrin_map_no は契約上 CRM が返すが、
# 照会役が落とす。残りはそもそも CRM が返さないと明言している。
FORBIDDEN = {"zenrin_map_no", "address", "latitude", "longitude",
             "deceased_name", "order_date", "phone"}

# 誰にも当たらないための名前。経路と認証だけを確かめる回に使う。
ABSENT_NAME = "ズズズノカミ"

# 実データに当たるための姓。ありふれた姓そのものは個人を指さない。
COMMON_SURNAME = os.environ.get("CRM_CHECK_SURNAME", "佐藤")

ASKED_BY = "acceptance-check"


def env_value(filename: str, key: str) -> str:
    path = os.path.join(HERE, filename)
    if not os.path.exists(path):
        return ""
    for line in io.open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line.startswith(key + "="):
            return line[len(key) + 1:].strip().strip('"')
    return ""


def ask(path: str, params: dict, token: str):
    url = f"{RELAY}{path}?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"X-Tachikoma-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
    except OSError as exc:
        return 0, {"error": f"{type(exc).__name__}: 照会役に繋がらない"}


def main() -> int:
    import sys
    sys.path.insert(0, HERE)
    import crm_bridge

    token = (os.environ.get("CRM_RELAY_TOKEN")
             or env_value(".env.crm_relay", "CRM_RELAY_TOKEN"))
    if not token:
        print("CRM_RELAY_TOKEN が読めない。.env.crm_relay を確認すること。")
        return 2

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # --- 経路と認証。誰にも当たらない照会で確かめる --------------------
    status, payload = ask("/crm/lookup", {"name": ABSENT_NAME,
                                          "asked_by": ASKED_BY}, token)
    check("該当なしの照会が 200 と 0件で返る",
          status == 200 and payload.get("count") == 0
          and payload.get("results") == [],
          f"status={status} count={payload.get('count')}")

    check("合言葉が違うと 403",
          ask("/crm/lookup", {"name": ABSENT_NAME, "asked_by": ASKED_BY},
              "wrong-token-on-purpose")[0] == 403)

    # --- 実データ。値は見ず、項目の名前だけ見る ------------------------
    status, payload = ask("/crm/lookup", {"name": COMMON_SURNAME,
                                          "asked_by": ASKED_BY}, token)
    rows = payload.get("results", [])
    total = payload.get("count", 0)
    keys = set()
    for row in rows:
        keys |= set(row.keys())

    check("実データが 200 で返る", status == 200 and total > 0,
          f"status={status} count={total} rows={len(rows)}")
    check("返る項目が許可した3つの内側に収まっている", keys <= ALLOWED,
          "項目名: " + (", ".join(sorted(keys)) or "(なし)"))
    check("住所・故人名・座標・地図番号は返らない", not (keys & FORBIDDEN),
          "混入: " + (", ".join(sorted(keys & FORBIDDEN)) or "なし"))
    check("読み上げ上限の3件を超えて返さない", len(rows) <= 3, f"rows={len(rows)}")

    # --- 照会した名前が、こちら側のログに残らない ----------------------
    if LOG and os.path.exists(LOG):
        text = io.open(LOG, encoding="utf-8-sig", errors="replace").read()
        leaked = [w for w in (COMMON_SURNAME, ABSENT_NAME) if w in text]
        check("照会した名前が照会役のログに残らない", not leaked,
              "漏れ: " + (", ".join(leaked) or "なし"))

    # --- 読み上げの組み立て。文そのものは出さない ----------------------
    if rows:
        spoken = crm_bridge._compose_reply(COMMON_SURNAME, total, rows)
        check("件数を正しく言う", f"{total}件" in spoken)
        check("多いときは下の名前を求める",
              "下の名前" in spoken or total <= 1)
        for index, row in enumerate(rows, start=1):
            cemetery = (row.get("cemetery_name") or "").strip()
            area = (row.get("area") or "").strip()
            check(f"墓所{index}を文に含む", cemetery in spoken)
            if area:
                check(f"墓所{index}が「地区の霊園」の形になる",
                      f"{area}地区の{cemetery}" in spoken)
            else:
                # area は実データの36%で空。欠落として喋ってはいけない。
                check(f"墓所{index}は地区が無くても欠落を言わない",
                      "分かりません" not in spoken and "不明" not in spoken)
        check("紙地図の図番を読み上げない",
              not any(k in spoken for k in ("ゼンリン", "図番")))
        check("文の長さが読み上げに耐える (200字以内)", len(spoken) <= 200,
              f"{len(spoken)}文字")

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'NG  '}{name.ljust(width)}  {detail}")
    print()
    print("すべて満たした" if not failed else f"{failed} 件が満たされていない")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
