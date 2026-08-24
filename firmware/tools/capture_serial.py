"""実機のログを、実機を再起動させずに読む。

**COM を普通に開くと ESP32 が再起動する。**DTR/RTS がリセット線に繋がって
いるため、pyserial が既定でそれをアサートした瞬間に機体が落ちる。調べたい
現象がそこで消えるので、ポートは未オープンで作り、`dtr`/`rts` を False に
してから開くこと。この1点がこのファイルの存在理由。

    python firmware/tools/capture_serial.py                    # Ctrl+C まで
    python firmware/tools/capture_serial.py --seconds 120      # 2分だけ
    python firmware/tools/capture_serial.py --grep TachikomaState,VoiceInput

書きながら流す (以前の使い捨て版は終了時にまとめて書き出していて、
「実行中は0バイトに見えるのが正常」という注意書きが要った)。要らない注意書きは
無くすほうがいい。

pyserial が要る。ESP-IDF の venv には esptool の依存として入っているので、
何も入れずに動く:

    C:\\Users\\user\\.espressif\\python_env\\idf5.5_py3.11_env\\Scripts\\python.exe
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys


def open_port(port: str, baud: int):
    try:
        import serial  # noqa: PLC0415 -- optional, and the error below is the point
    except ImportError:
        raise SystemExit(
            "pyserial が無い。ESP-IDF の python で実行すること:\n"
            r"  C:\Users\user\.espressif\python_env\idf5.5_py3.11_env"
            r"\Scripts\python.exe firmware/tools/capture_serial.py")

    # 未オープンで作ってから落とす。コンストラクタに port を渡すと
    # その場で開いてしまい、開いた時点で機体が再起動する。
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.timeout = 0.2
    handle.dtr = False
    handle.rts = False
    handle.open()
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=os.environ.get("TACHIKOMA_SERIAL_PORT", "COM3"))
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="0 なら Ctrl+C まで")
    parser.add_argument("--out", default="",
                        help="書き出し先。既定は logs/serial_<日時>.log")
    parser.add_argument("--grep", default="",
                        help="カンマ区切り。**画面に出す行だけ**を絞る。ファイルには全部残す")
    args = parser.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join("logs", f"serial_{stamp}.log")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    wanted = [w for w in args.grep.split(",") if w]

    handle = open_port(args.port, args.baud)
    started = datetime.datetime.now()
    print(f"{args.port} を {args.baud} で開いた (dtr/rts=False なので機体は再起動しない)")
    print(f"書き出し: {out}")
    if wanted:
        print(f"画面に出す行: {', '.join(wanted)}")
    print("止めるには Ctrl+C")
    print("-" * 60, flush=True)

    shown = total = 0
    try:
        with open(out, "w", encoding="utf-8", newline="\n") as sink:
            buffer = b""
            while True:
                if args.seconds and (datetime.datetime.now() - started).total_seconds() > args.seconds:
                    break
                chunk = handle.read(4096)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    total += 1
                    # 書きながら流す。落ちても、そこまでは残る。
                    sink.write(line + "\n")
                    sink.flush()
                    if not wanted or any(w in line for w in wanted):
                        shown += 1
                        print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        handle.close()

    print("-" * 60)
    print(f"{total} 行を書き出した" + (f" (うち {shown} 行を表示)" if wanted else ""))
    print(f"  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
