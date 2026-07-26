"""Fetch the three model files the biometrics need.

They are gitignored (54MB, and not ours to redistribute), so a fresh clone
has to pull them down. Run from the repository root:

    python firmware/docs/fetch_models.py

The resemblyzer *package* cannot be installed on Windows without a C++
toolchain -- its webrtcvad dependency needs one -- so gateway/biometrics.py
re-implements the network and only the weights are needed here. The two
OpenCV models are used through cv2's own FaceDetectorYN / FaceRecognizerSF,
which are built in to opencv-python.
"""
import os
import ssl
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.abspath(os.path.join(HERE, "..", "gateway", "models"))

FILES = [
    ("voice_encoder.pt", 17_000_000,
     "https://github.com/resemble-ai/Resemblyzer/raw/master/resemblyzer/pretrained.pt"),
    ("face_detection_yunet.onnx", 200_000,
     "https://github.com/opencv/opencv_zoo/raw/main/models/"
     "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    ("face_recognition_sface.onnx", 38_000_000,
     "https://github.com/opencv/opencv_zoo/raw/main/models/"
     "face_recognition_sface/face_recognition_sface_2021dec.onnx"),
]


def main() -> int:
    os.makedirs(DEST, exist_ok=True)
    context = ssl.create_default_context()
    failures = 0
    for name, expected, url in FILES:
        path = os.path.join(DEST, name)
        if os.path.exists(path) and os.path.getsize(path) > expected * 0.8:
            print(f"{name:32} already present ({os.path.getsize(path)/1e6:.1f} MB)")
            continue
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "tachikoma-setup"})
            with urllib.request.urlopen(request, timeout=300, context=context) as response:
                data = response.read()
            with open(path, "wb") as f:
                f.write(data)
            print(f"{name:32} downloaded ({len(data)/1e6:.1f} MB)")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"{name:32} FAILED {type(exc).__name__}: {str(exc)[:100]}")

    if failures:
        print("\nA corporate proxy that terminates TLS will break this the same way it"
              "\nbreaks pip. See firmware/docs/office-setup.md for the fix that does not"
              "\ninvolve turning certificate verification off.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
