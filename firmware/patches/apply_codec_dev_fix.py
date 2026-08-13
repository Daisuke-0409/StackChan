"""Re-apply the esp_codec_dev fix after the component manager fetches it again.

`managed_components/` is gitignored -- the IDF component manager owns it -- so
this change disappears the moment the component is re-downloaded, and the
symptom it fixes is severe enough that losing it silently would cost days.
It already did: the device crashed and rebooted mid-conversation for two weeks
before the panic was traced.

What it fixes
-------------
espressif__esp_codec_dev/platform/audio_codec_data_i2s.c contained

    ret = i2s_channel_reconfig_std_slot(channel, &slot_cfg);
    if (ret != ESP_OK) {
        *(int *) 0 = 0;
        return ESP_CODEC_DEV_DRV_ERR;
    }

A deliberate null write: a slot reconfiguration failure panicked instead of
returning. It fires in normal use. VoiceInputController::OpenFollowUp reopens
the microphone the instant a reply finishes, which reconfigures a channel the
playback path has not finished releasing; Core 1 died with StoreProhibited and
the device rebooted. The first exchange of a conversation worked and everything
after it did not -- which is how the hands-free bug presented.

Nothing else needed changing: TryOpenCodec() in cores3_audio_codec.cc already
retries once and then leaves the codec disabled rather than taking the device
down. It was never reached.

    python firmware/patches/apply_codec_dev_fix.py

Idempotent. Reports what it found either way, and exits non-zero only if the
file is missing or has changed shape enough that the edit cannot be placed --
which is the case that needs a human to look.
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(
    HERE, os.pardir, "managed_components", "espressif__esp_codec_dev",
    "platform", "audio_codec_data_i2s.c")

NEEDLE = "*(int *) 0 = 0;"

REPLACEMENT = """// Was `*(int *) 0 = 0;` -- a deliberate null write, so a slot
                // reconfiguration failure panicked instead of returning. It
                // fires in the field: reopening the mic at the instant a reply
                // finishes (VoiceInputController::OpenFollowUp) reconfigures a
                // channel the playback path has not finished releasing, and
                // Core 1 died with StoreProhibited mid-conversation. The device
                // rebooted, so the first exchange worked and everything after
                // it did not -- which is exactly how the hands-free bug
                // presented for two weeks.
                //
                // The caller already handles this properly: TryOpenCodec() in
                // cores3_audio_codec.cc retries once and then leaves the codec
                // disabled rather than taking the device down. It never got the
                // chance. Return the error and let it.
                ESP_LOGE(TAG, "i2s_channel_reconfig_std_slot failed: %d", ret);"""


def main() -> int:
    path = os.path.normpath(TARGET)
    if not os.path.exists(path):
        print(f"not found: {path}")
        print("Run a build first so the component manager fetches it.")
        return 1

    source = io.open(path, encoding="utf-8").read()
    if "i2s_channel_reconfig_std_slot failed" in source:
        print("already applied; nothing to do")
        return 0
    if NEEDLE not in source:
        print(f"the deliberate null write is not in {path}.")
        print("Either upstream fixed it -- check, and delete this script if so --")
        print("or the file changed shape and this needs a human.")
        return 1

    patched = source.replace(NEEDLE, REPLACEMENT, 1)
    io.open(path, "w", encoding="utf-8", newline="\n").write(patched)
    print(f"applied to {path}")
    print("Rebuild and reflash for it to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
