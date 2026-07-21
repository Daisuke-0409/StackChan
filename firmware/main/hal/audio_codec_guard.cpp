/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 */
#include "audio_codec_guard.h"

namespace stackchan::hal {

std::mutex& GetAudioCodecMutex()
{
    static std::mutex mutex;
    return mutex;
}

}  // namespace stackchan::hal
