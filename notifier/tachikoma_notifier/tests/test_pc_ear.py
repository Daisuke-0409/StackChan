"""UtteranceSegmenter: the firmware's hands-free bugs, as regression tests.

Each of these encodes a failure the robot's own ear actually shipped with in
August 2026. If the PC ear ever reproduces one, these fail with the bug's
name on them.
"""
import struct
import unittest

from tachikoma_notifier.pc_ear import FRAME_MS, FRAME_SAMPLES, UtteranceSegmenter


def frame(amplitude: int) -> bytes:
    return struct.pack(f"<{FRAME_SAMPLES}h", *([amplitude] * FRAME_SAMPLES))


LOUD = frame(8000)
QUIET = frame(100)


class Feeder:
    def __init__(self, segmenter: UtteranceSegmenter) -> None:
        self.segmenter = segmenter
        self.now = 0
        self.results: list[bytes] = []

    def feed(self, data: bytes, count: int) -> None:
        for _ in range(count):
            self.now += FRAME_MS
            out = self.segmenter.feed(data, self.now)
            if out is not None:
                self.results.append(out)


def build(**overrides) -> UtteranceSegmenter:
    values = dict(speech_rms=4000, silence_rms=2500, log=lambda _line: None)
    values.update(overrides)
    return UtteranceSegmenter(**values)


class SegmenterTests(unittest.TestCase):
    def test_normal_utterance_is_emitted_after_end_silence(self):
        feeder = Feeder(build())
        feeder.feed(LOUD, 50)    # 1s of speech
        feeder.feed(QUIET, 45)   # 900ms silence > 800ms threshold
        self.assertEqual(len(feeder.results), 1)

    def test_quiet_clock_keeps_running_during_speech(self):
        # THE 0ms bug: freezing quiet_since at onset measured every utterance
        # as zero and discarded it. Long speech must still come out.
        feeder = Feeder(build())
        feeder.feed(LOUD, 250)   # 5s of continuous speech
        feeder.feed(QUIET, 45)
        self.assertEqual(len(feeder.results), 1)

    def test_cough_is_ignored(self):
        feeder = Feeder(build())
        feeder.feed(LOUD, 5)     # 100ms burst < 300ms minimum
        feeder.feed(QUIET, 45)
        self.assertEqual(feeder.results, [])

    def test_max_duration_caps_the_utterance(self):
        # The 30-second bug: silence threshold below room tone meant no end.
        # Even if that recurs, the cap must emit rather than grow forever.
        feeder = Feeder(build(max_utterance_ms=2000))
        feeder.feed(LOUD, 150)   # 3s continuous
        self.assertEqual(len(feeder.results), 1)

    def test_word_gaps_do_not_split_a_sentence(self):
        feeder = Feeder(build())
        feeder.feed(LOUD, 25)    # 500ms word
        feeder.feed(QUIET, 20)   # 400ms gap < 800ms
        feeder.feed(LOUD, 25)    # next word
        feeder.feed(QUIET, 45)
        self.assertEqual(len(feeder.results), 1)

    def test_preroll_keeps_the_first_syllable(self):
        feeder = Feeder(build())
        feeder.feed(QUIET, 30)   # room tone fills the preroll ring
        feeder.feed(LOUD, 50)
        feeder.feed(QUIET, 45)
        [pcm] = feeder.results
        # 50 loud frames plus some preroll: strictly more than the speech alone.
        self.assertGreater(len(pcm), 50 * FRAME_SAMPLES * 2)


if __name__ == "__main__":
    unittest.main()
