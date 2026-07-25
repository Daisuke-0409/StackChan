"""Turning a voice clip or a camera frame into something comparable.

Both jobs produce the same shape of answer: a unit-length embedding that can
be cosine-compared against the samples in people.py. Neither decides who
somebody is -- that is PeopleStore.identify's job, and it refuses when the
answer is close.

Voice: the resemblyzer GE2E encoder. The package itself will not install on
this machine (its webrtcvad dependency needs a C++ toolchain that is not
here), but the network is a 3-layer LSTM and a linear projection, so it is
re-implemented below against the published weights. Same architecture, same
preprocessing, same embeddings.

Face: OpenCV's own FaceDetectorYN and FaceRecognizerSF, which ship inside
cv2 and only need their .onnx files. Detection is what the head-tracking
uses; recognition reuses the very same detection, so pointing the head at
somebody and knowing who they are cost one inference between them.

Everything is lazy: a gateway that never sees a face never pays for loading
a face model, and one running without the model files still answers chat
normally.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

MODEL_DIR = os.environ.get("TACHIKOMA_MODEL_DIR", os.path.join(os.path.dirname(__file__), "models"))

VOICE_MODEL = os.path.join(MODEL_DIR, "voice_encoder.pt")
FACE_DETECT_MODEL = os.path.join(MODEL_DIR, "face_detection_yunet.onnx")
FACE_RECOGNIZE_MODEL = os.path.join(MODEL_DIR, "face_recognition_sface.onnx")

# Resemblyzer's preprocessing constants. These are not tunable: the weights
# were trained against exactly this front end.
SAMPLE_RATE = 16000
MEL_WINDOW_LENGTH_MS = 25
MEL_WINDOW_STEP_MS = 10
MEL_CHANNELS = 40
TARGET_DBFS = -30.0
# 1.6s at a 10ms hop -- the window length the encoder was trained on.
PARTIAL_FRAMES = 160

# Below this there is not enough voice to characterize a speaker; a shorter
# clip produces an embedding that will happily match the wrong person.
MIN_VOICE_SECONDS = 1.2

_voice_lock = threading.Lock()
_voice_encoder: Any = None
_voice_failed = False

_face_lock = threading.Lock()
_face_detector: Any = None
_face_recognizer: Any = None
_face_failed = False


# --- voice ---------------------------------------------------------------
def _build_voice_encoder():
    import torch
    from torch import nn

    class VoiceEncoder(nn.Module):
        """Architecture of resemblyzer.VoiceEncoder, weights and all.

        Only the inference path: three stacked LSTMs, take the last layer's
        final hidden state, project, ReLU, and normalize to unit length so
        cosine similarity is the whole comparison.
        """

        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(MEL_CHANNELS, 256, 3, batch_first=True)
            self.linear = nn.Linear(256, 256)
            self.relu = nn.ReLU()

        def forward(self, mels):
            _, (hidden, _) = self.lstm(mels)
            raw = self.relu(self.linear(hidden[-1]))
            return raw / (torch.norm(raw, dim=1, keepdim=True) + 1e-9)

    checkpoint = torch.load(VOICE_MODEL, map_location="cpu", weights_only=False)
    state = checkpoint.get("model_state", checkpoint)
    # The checkpoint also carries the training-time similarity scale, which
    # has no place in a forward pass.
    state = {k: v for k, v in state.items() if k.startswith(("lstm.", "linear."))}
    model = VoiceEncoder()
    model.load_state_dict(state)
    model.eval()
    return model


def _get_voice_encoder():
    global _voice_encoder, _voice_failed
    with _voice_lock:
        if _voice_encoder is not None or _voice_failed:
            return _voice_encoder
        try:
            _voice_encoder = _build_voice_encoder()
        except Exception:
            _voice_failed = True
        return _voice_encoder


def voice_available() -> bool:
    return os.path.exists(VOICE_MODEL) and _get_voice_encoder() is not None


def voice_embedding(pcm: bytes, sample_rate: int) -> Optional[list[float]]:
    """Embed one clip of 16-bit mono PCM, or None if it cannot be used."""
    model = _get_voice_encoder()
    if model is None or not pcm:
        return None
    try:
        import librosa
        import numpy as np
        import torch

        wav = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if wav.size / float(sample_rate) < MIN_VOICE_SECONDS:
            return None
        if sample_rate != SAMPLE_RATE:
            wav = librosa.resample(wav, orig_sr=sample_rate, target_sr=SAMPLE_RATE)

        # Match the encoder's training loudness. A clip recorded quietly and
        # one recorded close up are the same speaker and must embed alike.
        rms = float(np.sqrt(np.mean(wav ** 2))) + 1e-9
        gain = 10 ** ((TARGET_DBFS - 20 * np.log10(rms)) / 20)
        wav = np.clip(wav * gain, -1.0, 1.0)

        # Drop leading/trailing near-silence so the embedding describes the
        # voice rather than the room.
        trimmed, _ = librosa.effects.trim(wav, top_db=30)
        if trimmed.size / SAMPLE_RATE >= MIN_VOICE_SECONDS:
            wav = trimmed

        mel = librosa.feature.melspectrogram(
            y=wav, sr=SAMPLE_RATE,
            n_fft=int(SAMPLE_RATE * MEL_WINDOW_LENGTH_MS / 1000),
            hop_length=int(SAMPLE_RATE * MEL_WINDOW_STEP_MS / 1000),
            n_mels=MEL_CHANNELS,
        ).astype(np.float32).T
        if mel.shape[0] < 40:
            return None

        # Partial utterances, not one pass over the whole clip. The encoder
        # was trained on fixed 1.6s windows, and an LSTM's final hidden
        # state after a long sequence is dominated by however the sentence
        # happened to end rather than by the voice saying it. Measured on
        # two distinct voices: whole-clip embeddings put different speakers
        # (max 0.813) *above* same-speaker pairs (min 0.708) -- unusable.
        # Averaging per-window embeddings is what the reference
        # implementation does, and what makes the space separable.
        windows = []
        step = PARTIAL_FRAMES // 2
        for start in range(0, max(1, mel.shape[0] - PARTIAL_FRAMES + 1), step):
            windows.append(mel[start:start + PARTIAL_FRAMES])
        if not windows or windows[-1].shape[0] < PARTIAL_FRAMES:
            tail = mel[-PARTIAL_FRAMES:]
            if tail.shape[0] >= min(PARTIAL_FRAMES, mel.shape[0]):
                windows.append(tail)
        windows = [w for w in windows if w.shape[0] >= 40]
        if not windows:
            windows = [mel]

        with torch.no_grad():
            batch = torch.from_numpy(np.stack([
                np.pad(w, ((0, PARTIAL_FRAMES - w.shape[0]), (0, 0)), mode="edge")
                if w.shape[0] < PARTIAL_FRAMES else w
                for w in windows
            ]))
            partials = model(batch).numpy()
        averaged = partials.mean(axis=0)
        averaged /= (np.linalg.norm(averaged) + 1e-9)
        return [float(v) for v in averaged]
    except Exception:
        return None


# --- face ----------------------------------------------------------------
def _get_face_models():
    global _face_detector, _face_recognizer, _face_failed
    with _face_lock:
        if _face_detector is not None or _face_failed:
            return _face_detector, _face_recognizer
        try:
            import cv2
            _face_detector = cv2.FaceDetectorYN.create(
                FACE_DETECT_MODEL, "", (320, 240), score_threshold=0.7)
            _face_recognizer = cv2.FaceRecognizerSF.create(FACE_RECOGNIZE_MODEL, "")
        except Exception:
            _face_failed = True
            _face_detector = None
            _face_recognizer = None
        return _face_detector, _face_recognizer


def face_available() -> bool:
    detector, _ = _get_face_models()
    return detector is not None


def detect_faces(image) -> list[dict[str, Any]]:
    """Every face in a BGR image, largest first.

    Each entry carries the box, the detector's confidence, and `center_x` /
    `center_y` normalized to -1..1 with the origin in the middle of the
    frame -- which is the form the head needs to turn toward it, so the
    caller never has to know the frame size.
    """
    detector, _ = _get_face_models()
    if detector is None or image is None:
        return []
    try:
        height, width = image.shape[:2]
        detector.setInputSize((width, height))
        _, faces = detector.detect(image)
        if faces is None:
            return []
        results = []
        for face in faces:
            x, y, w, h = (float(face[0]), float(face[1]), float(face[2]), float(face[3]))
            results.append({
                "box": [x, y, w, h],
                "score": float(face[-1]),
                "center_x": ((x + w / 2) / width) * 2.0 - 1.0,
                "center_y": ((y + h / 2) / height) * 2.0 - 1.0,
                "area_ratio": (w * h) / float(width * height),
                "_raw": face,
            })
        results.sort(key=lambda f: f["area_ratio"], reverse=True)
        return results
    except Exception:
        return []


def face_embedding(image, face) -> Optional[list[float]]:
    """Embed one detected face. `face` must come from detect_faces()."""
    detector, recognizer = _get_face_models()
    if recognizer is None or image is None or face is None:
        return None
    try:
        import numpy as np
        aligned = recognizer.alignCrop(image, np.asarray(face["_raw"], dtype=np.float32))
        feature = recognizer.feature(aligned)
        vector = feature.flatten().astype(float)
        norm = float(np.linalg.norm(vector)) + 1e-9
        return [float(v) for v in (vector / norm)]
    except Exception:
        return None


def decode_image(data: bytes):
    """JPEG (or any cv2-readable) bytes to a BGR image, or None."""
    try:
        import cv2
        import numpy as np
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None
