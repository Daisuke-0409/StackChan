import subprocess
import unittest
import wave

from tachikoma_notifier.windows_wave_synth import SpeechSynthesisError, synthesize_wav_pcm


def _write_wav(path: str, *, sample_rate: int, channels: int = 1, sampwidth: int = 2, frames: bytes = b"\x01\x00\x02\x00\x03\x00"):
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames)


def _runner_writing(*, channels=1, sampwidth=2, sample_rate_override=None, frames=b"\x01\x00\x02\x00\x03\x00", returncode=0, create_file=True):
    def runner(script, env):
        if create_file:
            rate = sample_rate_override if sample_rate_override is not None else int(env["TACHIKOMA_TTS_SAMPLE_RATE"])
            _write_wav(env["TACHIKOMA_TTS_OUT"], sample_rate=rate, channels=channels, sampwidth=sampwidth, frames=frames)
        return subprocess.CompletedProcess(args=["powershell.exe"], returncode=returncode, stdout="", stderr="")

    return runner


class SynthesizeWavPcmTests(unittest.TestCase):
    def test_happy_path_returns_raw_pcm_bytes(self):
        pcm = synthesize_wav_pcm("承認待ちだよ", sample_rate=24000, runner=_runner_writing())
        self.assertEqual(pcm, b"\x01\x00\x02\x00\x03\x00")

    def test_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            synthesize_wav_pcm("", runner=_runner_writing())

    def test_rejects_non_positive_sample_rate(self):
        with self.assertRaises(ValueError):
            synthesize_wav_pcm("hello", sample_rate=0, runner=_runner_writing())

    def test_nonzero_returncode_raises(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", runner=_runner_writing(returncode=1))

    def test_missing_output_file_raises(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", runner=_runner_writing(create_file=False))

    def test_stereo_output_is_rejected(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", sample_rate=24000, runner=_runner_writing(channels=2))

    def test_wrong_sample_width_is_rejected(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", sample_rate=24000, runner=_runner_writing(sampwidth=1))

    def test_mismatched_sample_rate_is_rejected(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", sample_rate=24000, runner=_runner_writing(sample_rate_override=16000))

    def test_empty_audio_is_rejected(self):
        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", sample_rate=24000, runner=_runner_writing(frames=b""))

    def test_subprocess_error_is_wrapped(self):
        def runner(script, env):
            raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=1)

        with self.assertRaises(SpeechSynthesisError):
            synthesize_wav_pcm("hello", runner=runner)

    def test_text_never_appears_in_raised_error_message(self):
        secret_like_text = "ひみつのフレーズ12345"
        try:
            synthesize_wav_pcm(secret_like_text, runner=_runner_writing(returncode=1))
            self.fail("expected SpeechSynthesisError")
        except SpeechSynthesisError as exc:
            self.assertNotIn(secret_like_text, str(exc))


if __name__ == "__main__":
    unittest.main()
