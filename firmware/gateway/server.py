"""Small provider-neutral Tachikoma Gateway.

The gateway owns provider credentials and conversation history.  The ESP32
receives only the bounded final response and never stores provider secrets.

Phase 5 adds a small, separate speech-announcement queue (POST /v1/speak,
GET /v1/speak_queue): a PC-side process (e.g. StackChanSpeechSink) enqueues
raw 16-bit PCM audio for one device, and the device polls for it. This queue
is single-slot per device_id (only the latest pending announcement is kept)
and holds no conversation history or provider credentials -- it is entirely
separate from /v1/chat.

Phase 5 also adds POST /v1/transcribe: the push-to-talk upload endpoint.
The device uploads one raw 16-bit PCM clip (recorded while a button was
held; see VoiceInputController on the firmware side) and gets back
recognized text, which it then feeds into its own /v1/chat request. This
endpoint owns the cloud STT provider credentials, exactly like /v1/chat
owns the AI provider credentials -- the device never sees an STT API key.
"""
from __future__ import annotations

import base64
import datetime
import json
import math
import os
import re
import shutil
import ssl
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

try:  # package import when run as gateway.server, plain when run as a script
    from . import biometrics, crm_bridge, order_bridge, people, settings_store, webui
except ImportError:  # pragma: no cover - depends on how the server is started
    import biometrics
    import crm_bridge
    import order_bridge
    import people
    import settings_store
    import webui

MAX_INPUT_BYTES = 512
MAX_OUTPUT_BYTES = 4096
MAX_SPEECH_AUDIO_BYTES = 720_000  # ~15s at 24kHz/16-bit/mono; sized for a real Gemini TTS reply,
                                   # not just the old fixed confirmation tone
MAX_TRANSCRIBE_AUDIO_BYTES = 1536 * 1024  # >= VoiceInputController's kMaxRecordingSamples (30s
                                          # at 24kHz mono = 1.44 MB), with headroom
# A 320x240 JPEG from the device's GC0308 is 10-25 KB; this is generous
# enough for a raw or high-quality frame without inviting a large upload.
MAX_VISION_IMAGE_BYTES = 512 * 1024

# Home-screen icon for the settings app. Inlined rather than kept as a file
# because the gateway is a couple of Python modules started by a script,
# and 3 KB of PNG is not worth an asset directory.
_APP_ICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAALqUlEQVR42u3dzTWDYRSF0bRgYqYCUw0oQJUKJQPmhLz35+ys"
    "XUDcxXm+GHB5eHwCINDFCQAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAA"
    "BAAAAQBAAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAE"
    "wBUABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAABAAAAQBA"
    "AAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAABMAVAAQAAAEAQAAAEAAA"
    "BAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAAAQAAAEAQAAAEAAABAAAAQBAAAAQ"
    "AAAEAAABAEAAABAAKPT88uoICACMn/KTHBwBgOVDLwwIAFh8PUAAwOLrAQIARl8MEAAw+mKAAIDRFwMEAOy+EiAA2H2UAAHA"
    "7qMECAB2HyVAADD9yAACgN1HCRAATD8ygABg+pEBBAC7jxIgAJh+ZAABwPQjAwgAph8ZQAAw/cgAAoD1RwMQAEy/DPjOFwAn"
    "MP3IAAKA6UcGEACsPxqAAGD6kQEEAOuPBiAAmH5kAAHA+qMBCACmHxlAALD+aAACgOlHBhAArD8agABg+pEBBADrjwYgAFh/"
    "NAABwPqjAQgAph8ZQACw/mgAAmD9QQMQAOsPGoAAWH/QAATA9IMMIADWHzQAAbD+oAEIgPUHDRAArD9ogABg/UEDBADrDxog"
    "AFh/0AABwPqDBggA1h80QACw/qABAoD1Bw0QAKw/aIAAYP1BAwQA6w8aIAAIAAKAAFh/0AAEwPqDBiAA1h80AAGw/qABCID1"
    "Bw1AAAQABAABsP6gAQiA9S/w9v5hv9xcAwSAnet/nZu/sNdurgECYP2DBkgM3FwDBEAAbJASuLkACID1t0FK4OYaIADW3wwl"
    "Z8DNNUAABMAMxWXAzQVAAKy/GYrLgJtrgABYfzMUlwE31wABEABLlNgANxcAAbD+ZiguA26uAQJg/S1RYgPcXAMEQAAsUWID"
    "3FwABMD6m6G4DLi5BgiA9bdEiQ1wcw0QAAGwRIkNcHMBEADrb4kSG+DmGiAA1t8SJTbAzTVAAATAEiXukZsLgABYf+ufuEdu"
    "rgECIAACIABuLgACYP0tUcweubkGCIAAWP/EPXLw2gYYHwGw/paoZo+cWgMEwPoLgAC4uV8ECYAAWP+YPXJkHwIEwPpb/8Q9"
    "cl4NEAABEAABQAAEwPpb/5g9clgNEAABEAABQAAEwPpb/5g9clINEAABEAABQAAEwPpb/2/X1+49cnMNEAABEICv6fnJSwDc"
    "XAAEQACWrP/Nr+l75OYjGmCgBMDjf68ZKp8kN/chAAEQgOIlqtojNxcABMD61y/R0D1ycw0QAAGIC8BdXwLg5gIgANa/6Rgd"
    "eAmAm2uAAAhA3KPooAdSN/chQAAEwON/6AOpmwuAAFj/oAAcfgmAm2uAAAhAizEqeYUHwM0FQACsf+KjaPMHUjfXAAEQAI//"
    "oQ+kbi4AAiAAAiAAbi4AAmD9BUAA3HxIADRAAARgwBKd2SM3H3FzARAAATBGAiAAAiAA1t8YCYAAaIAACIAxEgABEAABEABj"
    "JAACIAACkLH+xkgABEADBMDj/84larhHbu5DgAAIgAb4BODmPgEIgPUXAAFw84EB0AABEABj5OYCgAAIgDFycwEQACcQAGPk"
    "5gIgALRef2MkAAKgAQIgAP4ypb8G6q+BCoAACIAxEgABEAABEABjJAACIAACsHP9/X9a/xM45OYaIAACkPVA2nmJ3HzN+guA"
    "AAhAuz1q/ijq5gIgAAIQFICTe3TsK3LzQTcXAAGw/pVjdGaPTn45bj7o5hogAAKw/4F0yhK5+Zr1FwABEIAWezToUdTNBUAA"
    "BCAuAHeapPNfgpuPu7kACIAALNyjoUvk5jvWXwAEYPb6l4zRf+1RyTt3cwHQAAEQgMpJKnzPbi4AAiAAewJQu0e/WqXy9+nm"
    "1l8ABEAATsxTw3fl5gIgAAKwLQA992j9EjnpgvUXAAEYv/7GSAAEQAMEIDcA9qhkiRx2wfoLgAAIgDFycwEQAMYGwB6VLJHz"
    "Tl9/ARAAATBGbi4AAsDkANijkiVy5NHrLwACsCcA9qhkiZx67voLgAAIgDFycwEQAOs/fP3tUdUSOfjQ9dcAAdgWAHvk5tZf"
    "AAQgNwDJe+Tm1l8ABEAAjJGbC4AACEBkADL3yM2tvwAIgAAk7pGbW38BEAABSNwjN7f+AiAAApC4R27u5gIgAAKQuEdu7uYC"
    "IAACkLhHbu7mAiAAAhA3SW7u5gIgAAKQuEdu7uYCIAACkLhHbu7mAiAAAhA3SW7u5gIgAAKQuEdu7uYCIAACEDdJbu7mAiAA"
    "AhA3SW7u5gIgAAIQN0lu7uYCIADWP26S3NzNNUAANCBrlZzaza2/AAhA1io5rJsLgAAIQNAwOaCbC4AACMDaqXIENxcAARAA"
    "QAAEQAAAARAAAQAEQAAEABAAARAAQAAEQAAAARAAAQAEQAAEABAAARAAQAAEQAAAARAAAQAEQAAEABAAARAAQAAEQAAAARAA"
    "DQCsvwAIAAiAACAAIAACIACAAAiAAAACIAACAAiAAAgAIAACIACAAAiABgDWXwAEABAAARAAQAAEQAAAARAAAQAEQAA0ALD+"
    "AiAAgAAIgAAAAiAAAgAIgAAIACAAAqABgPUXAAEABEAABAAQAAEQAEAABEADAOsvAAIAAoAACAAIAAIgACAAAoAGgPUXAPy0"
    "gAAIgAAAAiAAAgAIgABoAGD9BUAAAAEQAAEABEAANACw/gIgAIAACIAAAAIgABoAWH8BEABAAARAAAABEAANAKy/AAgAIAAC"
    "IACAAAiABgDWXwAEABAAAdAAwPoLgACAACAAAgACgABoAFh/BEAAQAAEAA0A6y8ACAAIgACgAWD9BQABAAEQAAHwYwYCIAAa"
    "AFh/ARAAQAAEQAMA6y8AAgAIgABoAGD9BUAAAAEQAA0ArL8ACAAgAAKgAYD1FwANAKy/AAgAIAACoAFg/S2PAAgACAACoAFg"
    "/REAAQABQAA0AKw/AqABYP0RAAEAARAAJ9AAsP4CgAaA9RcABAAEQADQALD+AoAGgPUXAAQABEAA0ACw/gKABoD1FwAEAARA"
    "APBjDNZfADQAsP4CIACAAAiABgDWXwA0AKy/rRAADQDrjwBoAFh/BEAAQAAQAA0A648AaABYfwRAA8D6IwAaANYfAdAAsP4I"
    "gACAACAAGgDWXwDQALD+AoAGgPUXADQArL8AoAFg/QUADQDrLwBoAFh/AUADwPoLABoA1l8A0ACsPwKABmD9EQA0AOuPAKAB"
    "WH8EAA3A+iMAyACmHwFAA7D+CAAagPVHANAArD8CoAFg/REAGQDTjwBoAFh/BEADwPojABoA1h8BkAEw/QLgChoA1l8AkAEw"
    "/QKABoD1FwBkANOPAKABWH8EABnA9CMAaADWHwFABjD9CAAagPVHAJABTD8CgAxg+hEAZADTjwCgAVh/BAAZwPQjAMgAph8B"
    "QAYw/QgAMoDpRwBQAuw+AoAMmH7fmQgAMmD6QQBQArsPAoAMmH4QAJTA7oMAoAR2HwEAJbD7CAAogd1HAEAMjD4CAGJg9BEA"
    "EAOjjwCAHlh8BAD0wOIjALA+DA6OAMD4bDgCAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAAC"
    "AIAAACAAAAgAAAIAgAAAIAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAAHCrT/wH"
    "jY41Y8H2AAAAAElFTkSuQmCC"
)

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
MIN_TRANSCRIBE_AUDIO_SECONDS = 0.5  # below this, skip STT entirely and treat as "didn't hear
                                    # anything" -- confirmed live: Gemini's STT confidently
                                    # hallucinates a fluent, plausible-sounding but entirely
                                    # fabricated sentence for near-empty audio instead of
                                    # admitting it heard nothing, so a low-confidence real
                                    # result isn't the failure mode being guarded against here

# --- TEMPORARY debug instrumentation (2026-07-24, "STT sounds wrong" /
# latency investigation) ---------------------------------------------------
# Off by default: recognized speech content and raw mic audio are personal
# conversation data, so this must never log or save anything unless a human
# explicitly opts in for one debugging session. Remove this block (and its
# call sites in process_transcribe/_log) once the investigation is done.
def _debug_logging_enabled(env: dict[str, str]) -> bool:
    return env.get("TACHIKOMA_DEBUG_LOGGING") == "1"


DEBUG_AUDIO_DIR = os.environ.get(
    "TACHIKOMA_DEBUG_AUDIO_DIR", os.path.join(tempfile.gettempdir(), "tachikoma_debug_audio")
)
# --- end temporary debug instrumentation block (see other markers below) --


def _log(message: str) -> None:
    """Prints one gateway log line with a wall-clock timestamp, so it can be
    correlated against the device's own epoch-ms log timestamps. Replaces
    bare print() for all gateway logging (access log, streaming/enqueue
    diagnostics); never includes conversation content by default.
    """
    print(f"[{datetime.datetime.now().isoformat(timespec='milliseconds')}] {message}")

# Each device_id maps to an ordered list, drained front-first by
# dequeue_speech(). enqueue_speech() defaults to *replacing* that list with
# a single new item -- the original Phase 5 contract POST /v1/speak and its
# callers (StackChanSpeechSink) still rely on ("only the latest pending
# announcement is kept"), verified against real hardware and covered by
# test_second_enqueue_replaces_first_pending_one. append=True is additive
# only, used by the streaming chat->TTS path (see
# _gemini_stream_chat_and_speak) to queue several sentences in speaking
# order without one clobbering the last.
_speech_queue: dict[str, list[bytes]] = {}
_speech_queue_lock = threading.Lock()

# --- conversation memory -------------------------------------------------
# Each /v1/chat request used to be sent to Gemini entirely on its own: the
# device's session_id was echoed back and never used for anything, so the
# assistant could not remember a name it had just been told, and asking about
# local weather meant repeating your address every single time.
#
# Two layers, because they answer different questions:
#   turns   -- the last few exchanges, so follow-ups like "じゃあ明日は?"
#              resolve against what was just said.
#   profile -- durable facts (name, where they live, preferences) that should
#              still be known next week. Extracted in the background after a
#              reply has already been sent, so remembering costs the user no
#              waiting.
#
# Stored under MEMORY_DIR. This is personal data: keep it local, out of git,
# and out of the access log.
#
# One persona, several bodies. The store used to be named after the device
# (its MAC), which put the project's own premise the wrong way round: a second
# robot would recognise the same person -- people.json has never been
# per-device -- and still remember nothing that was ever said to the first
# one. Same face, same voice, no shared past. Every body now reads and writes
# the same store; TACHIKOMA_MEMORY_SCOPE=device restores the old split for
# anyone who genuinely wants two personas rather than one with two bodies.
#
# MEMORY_DIR may point at a share (a NAS) so the store outlives any single PC.
# Run only ONE gateway against a file-backed store: _save_memory() replaces
# the file atomically on a local filesystem, and that guarantee does not carry
# across SMB, so two writers would quietly drop each other's turns. Several
# gateways at once need a database rather than a shared folder.
MEMORY_DIR = os.environ.get("TACHIKOMA_MEMORY_DIR", os.path.join(os.path.dirname(__file__), "memory"))
MEMORY_SCOPE = os.environ.get("TACHIKOMA_MEMORY_SCOPE", "shared").strip().lower()
MEMORY_BRAIN_ID = os.environ.get("TACHIKOMA_BRAIN_ID", "tachikoma").strip() or "tachikoma"
MEMORY_MAX_TURNS = 20  # 10 exchanges
MEMORY_MAX_PROFILE_ITEMS = 40
MEMORY_MAX_FACT_CHARS = 200
_memory_lock = threading.Lock()
# Paths whose one-time adoption of a pre-sharing store has been considered.
# Guarded by _memory_lock, like everything else that touches the store.
_adopted_legacy: set[str] = set()

PEOPLE_PATH = os.environ.get("TACHIKOMA_PEOPLE_FILE",
                             os.path.join(os.path.dirname(__file__), "memory", "people.json"))
_people_store = people.PeopleStore(PEOPLE_PATH)

# Who the device is currently talking to, per device. Identification happens
# on the audio upload; the chat request that follows is a separate HTTP call
# and has no voice of its own to go on, so the answer is carried here.
#
# It expires. Somebody else picking up the conversation ten minutes later
# must not inherit the last speaker's standing, and the safe direction when
# in doubt is to forget rather than to keep trusting.
SPEAKER_TTL_SECONDS = float(os.environ.get("SPEAKER_TTL_SECONDS", "180"))
_speaker_lock = threading.Lock()
_current_speaker: dict[str, dict[str, Any]] = {}


def set_current_speaker(device_id: str, person: Optional[dict[str, Any]], score: float,
                        modality: str) -> None:
    if not device_id:
        return
    with _speaker_lock:
        if person is None:
            _current_speaker.pop(device_id, None)
        else:
            _current_speaker[device_id] = {
                "person_id": person["id"], "name": person.get("name", ""),
                "role": person.get("role", people.ROLE_GUEST),
                "score": score, "modality": modality, "at": time.time(),
            }


def get_current_speaker(device_id: str) -> Optional[dict[str, Any]]:
    with _speaker_lock:
        entry = _current_speaker.get(device_id)
        if entry is None:
            return None
        if time.time() - entry["at"] > SPEAKER_TTL_SECONDS:
            _current_speaker.pop(device_id, None)
            return None
        return dict(entry)


def current_role(device_id: str) -> str:
    entry = get_current_speaker(device_id)
    if entry:
        return entry["role"]
    # Nobody enrolled yet means this is still a single-user device, and has
    # been for its whole life: every fact in memory was told to it by the
    # one person who uses it. Treating that person as a stranger would hide
    # his own name and address from him and quietly break what already
    # works. Access control starts mattering the moment there is somebody to
    # tell apart -- i.e. once anyone has enrolled.
    if not _people_store.list_people():
        return people.ROLE_MASTER
    return people.ROLE_UNKNOWN


def _memory_key(device_id: str) -> str:
    """Whose memory a body reads and writes.

    Shared by default, so which robot happens to be in the room stops being
    part of the robot's identity. The device is still the unit of *routing* --
    who to speak to, which head to move -- but no longer the unit of memory.
    """
    if MEMORY_SCOPE == "device":
        return device_id
    return MEMORY_BRAIN_ID


def _memory_path(device_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", _memory_key(device_id))[:64] or "unknown"
    # people.json holds every enrolled voiceprint and face, and settings.json
    # the operator's configuration. A brain id that landed on either would
    # overwrite it with conversation history and undo every enrolment, so a
    # brain never gets to own those two names.
    if f"{safe}.json" in (os.path.basename(PEOPLE_PATH),
                          os.path.basename(settings_store.SETTINGS_PATH)):
        safe = f"brain_{safe}"
    return os.path.join(MEMORY_DIR, f"{safe}.json")


def _adopt_legacy_device_store(path: str) -> None:
    """Carry a pre-sharing, device-named store over to the shared one. Once.

    Without this, turning sharing on reads as amnesia rather than as a
    settings change: the same people are still enrolled, and yet yesterday is
    gone. Copies rather than moves, so the old file stays put as a fallback.

    Ambiguity is left alone deliberately. Two device stores mean two histories
    that a machine cannot interleave without inventing an order for them, and
    guessing wrong here writes a false past into the only place the robot
    trusts.
    """
    if MEMORY_SCOPE == "device" or path in _adopted_legacy:
        return
    _adopted_legacy.add(path)
    if os.path.exists(path):
        return
    try:
        names = [n for n in os.listdir(MEMORY_DIR) if n.endswith(".json")]
    except OSError:
        return
    reserved = {os.path.basename(PEOPLE_PATH),
                os.path.basename(settings_store.SETTINGS_PATH),
                os.path.basename(path)}
    candidates = sorted(n for n in names if n not in reserved)
    if not candidates:
        return
    if len(candidates) > 1:
        _log(f"gateway memory: {len(candidates)} device stores predate sharing; "
             f"adopting none, merge by hand to keep that history")
        return
    try:
        shutil.copyfile(os.path.join(MEMORY_DIR, candidates[0]), path)
    except OSError as exc:
        _log(f"gateway memory: could not adopt {candidates[0]}: {type(exc).__name__}")
        return
    _log(f"gateway memory: adopted {candidates[0]} as the shared store")


def _load_memory(device_id: str) -> dict[str, Any]:
    path = _memory_path(device_id)
    _adopt_legacy_device_store(path)
    try:
        # utf-8-sig for the same reason as the VOICEVOX dictionary: these
        # files get corrected by hand (a name STT spelled wrong, a fact to
        # drop), and a Windows editor's BOM would otherwise make the whole
        # memory look unreadable and silently start over from empty.
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"profile": [], "turns": []}
    # A fact is {"text": ..., "visibility": ...}. Files written before
    # visibility existed hold bare strings; those predate anyone but the
    # operator ever being identified, so they are read back as master-only --
    # the closed direction, which can be opened deliberately but never
    # leaks by accident.
    profile: list[dict[str, Any]] = []
    for item in data.get("profile", []):
        if isinstance(item, str):
            profile.append({"text": item, "visibility": people.VISIBILITY_MASTER})
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            visibility = item.get("visibility")
            profile.append({
                "text": item["text"],
                "visibility": visibility if visibility in people._VISIBILITY_MIN_RANK
                else people.VISIBILITY_MASTER,
            })
    turns = []
    for t in data.get("turns", []):
        if isinstance(t, dict) and t.get("role") in ("user", "model") and isinstance(t.get("text"), str):
            visibility = t.get("visibility")
            turns.append({
                "role": t["role"], "text": t["text"],
                "visibility": visibility if visibility in people._VISIBILITY_MIN_RANK
                else people.VISIBILITY_MASTER,
            })
    return {"profile": profile[:MEMORY_MAX_PROFILE_ITEMS], "turns": turns[-MEMORY_MAX_TURNS:]}


def _save_memory(device_id: str, memory: dict[str, Any]) -> None:
    path = _memory_path(device_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-replace: a crash mid-write must not leave a truncated
        # file that _load_memory would silently read as "no memory at all".
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        _log(f"gateway memory save failed: {type(exc).__name__}")


def _memory_prompt_parts(device_id: str, role: str = people.ROLE_MASTER) -> tuple[str, list[dict[str, Any]]]:
    """Returns (system-instruction suffix, prior conversation contents).

    Filtered by who is listening. This is the whole access-control
    mechanism: a fact the current speaker may not hear is not included, so
    the model composing the answer never had it. Telling the model to keep
    quiet about something it can see is not the same thing -- a
    sympathetic-sounding question can still draw that out.
    """
    with _memory_lock:
        memory = _load_memory(device_id)
    audible = [f for f in memory["profile"] if people.can_hear(role, f["visibility"])]
    withheld = len(memory["profile"]) - len(audible)
    suffix = ""
    if audible:
        remembered = "\n".join(f"- {fact['text']}" for fact in audible)
        suffix = (
            "\n\n以下はこのユーザーについて記憶している情報です。"
            "質問に答えるときは、必要に応じてこの情報を前提として使ってください"
            "（例えば天気を聞かれたら、記憶している居住地の天気を答えてください）。"
            "ただし、聞かれてもいないのにこの情報をわざわざ復唱しないでください。\n"
            f"{remembered}"
        )
    contents = [{"role": turn["role"], "parts": [{"text": turn["text"]}]}
                for turn in memory["turns"] if people.can_hear(role, turn["visibility"])]
    if withheld:
        # Counts only. Logging which facts were withheld would put them in
        # the log, which is the one place this design is trying to keep them
        # out of.
        _log(f"gateway memory filtered role={role} withheld_facts={withheld}")
    return suffix, contents


def _append_turn(device_id: str, user_text: str, model_text: str, visibility: str) -> None:
    with _memory_lock:
        memory = _load_memory(device_id)
        memory["turns"].append({"role": "user", "text": user_text, "visibility": visibility})
        memory["turns"].append({"role": "model", "text": model_text, "visibility": visibility})
        memory["turns"] = memory["turns"][-MEMORY_MAX_TURNS:]
        _save_memory(device_id, memory)


def _restrict_recent_turns(device_id: str, count: int = 4) -> int:
    """Pull the last few exchanges back to master-only.

    For "さっきの話は内緒ね" -- the cue arrives *after* the thing it is about
    has already been stored, so marking only what follows would miss the
    point entirely.
    """
    changed = 0
    with _memory_lock:
        memory = _load_memory(device_id)
        for turn in memory["turns"][-count:]:
            if turn["visibility"] != people.VISIBILITY_MASTER:
                turn["visibility"] = people.VISIBILITY_MASTER
                changed += 1
        for fact in memory["profile"]:
            if fact["visibility"] != people.VISIBILITY_MASTER:
                fact["visibility"] = people.VISIBILITY_MASTER
                changed += 1
        if changed:
            _save_memory(device_id, memory)
    return changed


_PROFILE_EXTRACT_PROMPT = (
    "あなたは会話から、ユーザーに関する長期的に覚えておくべき事実だけを抽出する係です。"
    "名前、居住地、家族、仕事、好み、繰り返し使う設定などが対象です。"
    "その場限りの話題、天気、時刻、雑談の内容は含めないでください。"
    "既存の事実と矛盾する新しい情報があれば、新しい方に置き換えてください。"
    "出力は事実を表す短い日本語の文字列のJSON配列だけとし、説明は書かないでください。"
    "覚えるべきことが何もなければ、既存の配列をそのまま返してください。"
)


def _update_profile(device_id: str, user_text: str, model_text: str, env: dict[str, str],
                    visibility: str = people.VISIBILITY_MASTER) -> None:
    """Refresh the durable facts for this device. Runs on a background thread
    after the reply has been sent -- never in the request path."""
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return
    with _memory_lock:
        existing = _load_memory(device_id)["profile"]
    existing_texts = [f["text"] for f in existing]
    model = env.get("MEMORY_MODEL", GEMINI_DEFAULT_CHAT_MODEL)
    request_body = json.dumps({
        "system_instruction": {"parts": [{"text": _PROFILE_EXTRACT_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text":
            f"既存の事実:\n{json.dumps(existing_texts, ensure_ascii=False)}\n\n"
            f"直近の会話:\nユーザー: {user_text}\nアシスタント: {model_text}"}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode("utf-8")
    request = urllib.request.Request(f"{GEMINI_API_BASE_URL}/models/{model}:generateContent",
                                     data=request_body, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_OUTPUT_BYTES * 4).decode("utf-8"))
        parts = decoded["candidates"][0]["content"]["parts"]
        facts = json.loads("".join(p["text"] for p in parts if isinstance(p.get("text"), str)))
    except Exception:
        # Memory is best-effort: never let this break or delay a conversation.
        return
    if not isinstance(facts, list):
        return
    cleaned: list[str] = []
    for fact in facts:
        if isinstance(fact, str) and fact.strip():
            cleaned.append(fact.strip()[:MEMORY_MAX_FACT_CHARS])
    cleaned = cleaned[:MEMORY_MAX_PROFILE_ITEMS]
    if cleaned == [f["text"] for f in existing]:
        return
    # Keep the visibility already decided for a fact we have seen before;
    # a re-extraction must not quietly widen something that was marked
    # private. Genuinely new facts take this turn's visibility.
    previous = {f["text"]: f["visibility"] for f in existing}
    with _memory_lock:
        memory = _load_memory(device_id)
        memory["profile"] = [
            {"text": text, "visibility": previous.get(text, visibility)} for text in cleaned
        ]
        _save_memory(device_id, memory)
    # Counts only -- the facts themselves are personal and stay out of the log.
    _log(f"gateway memory profile updated device_id={device_id} facts={len(cleaned)} "
         f"visibility={visibility}")


def _visibility_for_turn(device_id: str, user_text: str) -> str:
    """How widely what was just said may be repeated.

    Starts from who is speaking -- what the operator says is his to keep,
    what a colleague volunteers about themselves is not the household's
    secret -- and then narrows if the words themselves asked for it.
    """
    role = current_role(device_id)
    visibility = people.default_visibility_for(role)
    # The operator reported saying "ここだけの話" and nothing happened, and
    # there was no way to tell whether the cue had missed or the
    # transcription had. Logging the decision (never the words -- that is the
    # one thing this feature exists to keep quiet) makes the difference
    # visible next time.
    _log(f"gateway visibility decision role={role} secret_cue={people.mentions_secret(user_text)} "
         f"open_cue={people.mentions_unsecret(user_text)} chars={len(user_text)}")
    if people.mentions_secret(user_text):
        visibility = people.VISIBILITY_MASTER
        # The cue usually refers to what was *just* said, not only to what
        # comes next, so pull the recent exchanges closed too.
        changed = _restrict_recent_turns(device_id)
        _log(f"gateway privacy cue -> master-only (also restricted {changed} earlier entries)")
    elif people.mentions_unsecret(user_text) and role == people.ROLE_MASTER:
        visibility = people.VISIBILITY_HOUSEHOLD
        _log("gateway privacy cue -> opened to household")
    return visibility


def _remember_exchange(device_id: str, user_text: str, model_text: str, env: dict[str, str]) -> None:
    if not _memory_enabled(env) or not device_id or not model_text:
        return
    visibility = _visibility_for_turn(device_id, user_text)
    _append_turn(device_id, user_text, model_text, visibility)
    threading.Thread(target=_update_profile,
                     args=(device_id, user_text, model_text, env, visibility),
                     daemon=True).start()


def enqueue_speech(device_id: str, audio: bytes, *, append: bool = False) -> tuple[int, dict[str, Any]]:
    """Store a pending announcement for device_id.

    append=False (default): replaces any existing queue for this device
    with just this one item -- the original single-slot behavior.
    append=True: adds to the end of the existing queue instead.
    """
    if not isinstance(device_id, str) or not device_id:
        return _result(400, "invalid_input")
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) % 2 != 0:
        return _result(400, "invalid_input")
    if len(audio) > MAX_SPEECH_AUDIO_BYTES:
        return _result(413, "invalid_input")
    with _speech_queue_lock:
        if append and device_id in _speech_queue:
            _speech_queue[device_id].append(bytes(audio))
        else:
            _speech_queue[device_id] = [bytes(audio)]
    return 200, {"ok": True}


def dequeue_speech(device_id: str) -> Optional[bytes]:
    """Pop and return the oldest pending announcement for device_id, if any."""
    with _speech_queue_lock:
        pending = _speech_queue.get(device_id)
        if not pending:
            _speech_queue.pop(device_id, None)
            return None
        audio = pending.pop(0)
        if not pending:
            del _speech_queue[device_id]
        return audio


def _result(status: int, code: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    body = {"error": code}
    body.update(extra)
    return status, body


def _authorized(headers: dict[str, str], env: dict[str, str]) -> bool:
    expected = env.get("DEVICE_TOKEN", "")
    # Header names are case-insensitive on the wire (an iPhone Shortcut
    # typed as "authorization" is just as valid), but these headers arrive
    # as a plain dict whose lookup isn't. Fold the key, not the value.
    supplied = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not expected:
        return env.get("AI_PROVIDER", "mock") == "mock" and env.get("ALLOW_INSECURE_DEV") == "1"
    return supplied == f"Bearer {expected}"


GEMINI_DEFAULT_CHAT_MODEL = "gemini-3.5-flash-lite"
GEMINI_DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_DEFAULT_TTS_VOICE = "Zephyr"  # chosen by ear over Kore + 6 alternates against real Japanese text
GEMINI_DEFAULT_STT_MODEL = "gemini-3.5-flash-lite"  # gemini-2.5-flash 404'd ("no longer available
                                                     # to new users") when verified live 2026-07-24.
                                                     # gemini-flash-latest worked but measured
                                                     # 3312ms on a 2.8s clip against this model's
                                                     # 1859ms, and STT sits directly in the user's
                                                     # wait for a reply. The one accuracy gap this
                                                     # model had ("立ちコマ" for "タチコマ") is
                                                     # closed by the name hint in _GEMINI_STT_PROMPT.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The name hint is not cosmetic: without it gemini-3.5-flash-lite transcribed
# "タチコマ" as "立ちコマ"/"たちこま", and the device's own name is the single
# most likely word in any utterance addressed to it. With the hint both test
# phrases came back exactly right, and measurably faster (1859ms vs 2336ms).
_GEMINI_STT_PROMPT = (
    "次の音声を一字一句そのまま日本語で書き起こしてください。"
    "話者は「タチコマ」という名前のロボットに話しかけています。"
    "書き起こしたテキストのみを返し、説明や前置きは付けないでください。"
)


def _stt_prompt(env: dict[str, str]) -> str:
    """The transcription prompt, plus any locally configured spellings.

    A name has no single correct kanji from audio alone: "ダイスケ" came back
    as 大助 and was stored in the profile that way, so the assistant then used
    the wrong characters for its owner's name in every reply. STT_VOCABULARY
    (comma-separated) lists the spellings this household actually uses. It
    lives in .env rather than in this file because it is personal data.
    """
    vocabulary = [w.strip() for w in env.get("STT_VOCABULARY", "").split(",") if w.strip()]
    if not vocabulary:
        return _GEMINI_STT_PROMPT
    return (_GEMINI_STT_PROMPT
            + "次の固有名詞が出てきた場合は、必ずこの表記を使ってください: "
            + "、".join(vocabulary) + "。")
# Every word of a reply is read aloud, so formatting is not cosmetic here:
# with web search enabled Gemini answers in markdown by default ("**気温**:",
# "* 項目") and a bulleted list of headlines is unusable as speech. Keep the
# instruction plain rather than a persona -- 781215a deliberately removed the
# styled prompt -- but state the spoken-output constraints explicitly.
GEMINI_SYSTEM_PROMPT = (
    "日本語で、簡潔に答えてください。"
    "回答はそのまま音声で読み上げられます。"
    "箇条書き、マークダウン記法、アスタリスクなどの記号、URL、絵文字は使わず、"
    "話し言葉の文章だけで答えてください。"
    "調べた内容を伝えるときも、要点を2〜3文にまとめてください。"
    # The tag is how the robot's body learns what the reply felt like. It is
    # asked for first so it arrives in the opening stream chunk, before any
    # sentence has finished -- the motion can then start with the speech
    # instead of after it. _EMOTION_TAG_RE strips it before TTS.
    "回答の先頭に、その回答の感情を [happy] [sad] [neutral] のいずれか一つで"
    "必ず付けてください。嬉しい・楽しい・褒められたときは happy、"
    "残念・わからない・失敗を伝えるときは sad、それ以外は neutral です。"
    "タグの後に続けて、普通に回答を書いてください。"
)

# Deliberately tolerant: the model sometimes emits 【happy】 or "happy:" or
# wraps the tag in whitespace. Anything that fails to match simply stays in
# the text and is treated as neutral, which is the safe direction.
_EMOTION_TAG_RE = re.compile(r"^\s*[\[\【]?\s*(happy|sad|neutral)\s*[\]\】]?\s*[:：]?\s*", re.IGNORECASE)
# Matches a *prefix of* a tag, so a chunk that ends mid-tag ("[hap") is not
# mistaken for a reply that simply has no tag.
_EMOTION_TAG_MAYBE_RE = re.compile(
    r"^\s*[\[\【]?\s*(h(a(p(p(y)?)?)?)?|s(a(d)?)?|n(e(u(t(r(a(l)?)?)?)?)?)?)$", re.IGNORECASE)

# Reaction names the firmware understands (TachikomaReaction). "neutral"
# deliberately maps to nothing -- most replies should not make it dance.
_EMOTION_TO_REACTION = {"happy": "happy", "sad": "confused"}

# One pending reaction per device, consumed by the next /v1/speak_queue fetch
# so the motion starts exactly when the audio does.
_pending_emotion: dict[str, str] = {}
_pending_emotion_lock = threading.Lock()

# Manner mode: stop moving, keep talking. Asked for out loud rather than by
# gesture, because every head-touch gesture is already taken -- press and
# release are push-to-talk, and the swipes are the petting reaction.
_MANNER_ON_RE = re.compile(r"(マナーモード|静かにして|動かないで|じっとして)(?!.*(解除|やめ|off|オフ|終わ))")
_MANNER_OFF_RE = re.compile(r"(マナーモード|静か).*(解除|やめて|終わり|オフ|off)|(動いて(いい|ok|OK)|普通に戻)")
_pending_command: dict[str, str] = {}
_pending_command_lock = threading.Lock()


# Last known phone location (R-mobile-order FR-2): posted by an iPhone
# shortcut over the tailnet, read by the order bridge when a job starts.
# One slot, newest wins; nothing here is persisted across restarts.
_last_location: dict[str, float] = {}
_last_location_lock = threading.Lock()


def set_last_location(lat: float, lng: float) -> None:
    with _last_location_lock:
        _last_location.clear()
        _last_location.update({"lat": lat, "lng": lng, "ts": time.time()})


def get_last_location(max_age_seconds: float = 3600.0) -> Optional[dict[str, float]]:
    """The last reported location, or None if stale/absent. Staleness
    matters: ordering at the nearest store to where the phone was this
    morning is worse than falling back to the default store."""
    with _last_location_lock:
        if not _last_location:
            return None
        if time.time() - _last_location["ts"] > max_age_seconds:
            return None
        return dict(_last_location)


def detect_manner_command(text: str) -> Optional[str]:
    """"motion_off" / "motion_on" / None for what was just said."""
    if not text:
        return None
    if _MANNER_OFF_RE.search(text):
        return "motion_on"
    if _MANNER_ON_RE.search(text):
        return "motion_off"
    return None


def set_pending_command(device_id: str, command: Optional[str]) -> None:
    if not device_id:
        return
    with _pending_command_lock:
        if command:
            _pending_command[device_id] = command
        else:
            _pending_command.pop(device_id, None)


def take_pending_command(device_id: str) -> Optional[str]:
    with _pending_command_lock:
        return _pending_command.pop(device_id, None)


def _split_emotion_tag(text: str) -> tuple[str, Optional[str]]:
    """Pulls a leading [happy]/[sad]/[neutral] tag off a reply.

    Returns (text without the tag, reaction name or None).
    """
    match = _EMOTION_TAG_RE.match(text)
    if not match:
        return text, None
    return text[match.end():], _EMOTION_TO_REACTION.get(match.group(1).lower())


def set_pending_emotion(device_id: str, reaction: Optional[str]) -> None:
    if not device_id:
        return
    with _pending_emotion_lock:
        if reaction:
            _pending_emotion[device_id] = reaction
        else:
            _pending_emotion.pop(device_id, None)


def take_pending_emotion(device_id: str) -> Optional[str]:
    with _pending_emotion_lock:
        return _pending_emotion.pop(device_id, None)

# google_search lets Gemini look things up when the answer needs current or
# external information. Verified live 2026-07-26 with gemini-3.5-flash-lite:
# it searches only when warranted ("今日の東京の天気" -> 2 queries, 3266ms)
# and skips it otherwise ("こんにちは、元気？" -> no search, 953ms), so
# leaving it on costs ordinary conversation nothing.
def _web_search_enabled(env: dict[str, str]) -> bool:
    # The .env switch is the hard off; the app toggle is the everyday one, so
    # either being off means off.
    if env.get("ENABLE_WEB_SEARCH", "1") != "1":
        return False
    return bool(settings_store.get("web_search_enabled"))


def _memory_enabled(env: dict[str, str]) -> bool:
    if env.get("ENABLE_MEMORY", "1") != "1":
        return False
    return bool(settings_store.get("memory_enabled"))


def _speaker_id_enabled(env: dict[str, str]) -> bool:
    if env.get("ENABLE_SPEAKER_ID", "1") != "1":
        return False
    return bool(settings_store.get("speaker_id_enabled"))


def _persona_suffix() -> str:
    """Turns the operator's persona settings into prompt text.

    Empty settings add nothing at all rather than a paragraph saying there is
    no persona -- an instruction the model still has to read and weigh.
    """
    values = settings_store.load()
    parts = []
    first_person = str(values.get("first_person", "")).strip()
    if first_person:
        parts.append(f"あなたの一人称は「{first_person}」です。必ずこれを使ってください。")
    persona = str(values.get("persona", "")).strip()
    if persona:
        parts.append(f"あなたの性格と話し方: {persona}")
    return ("\n" + "\n".join(parts)) if parts else ""


def _chat_tools(env: dict[str, str]) -> list[dict[str, Any]]:
    return [{"google_search": {}}] if _web_search_enabled(env) else []


_MARKUP_RE = re.compile(r"[*_`#>|]+")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")


def _speakable(text: str) -> str:
    """Strip markup that would otherwise be pronounced.

    The system prompt asks for plain speech, but a grounded answer still
    slips in the occasional "**" or bare URL, and TTS reads those out
    literally. Belt and braces: the prompt keeps replies clean, this keeps
    the ones that are not from reaching the speaker.
    """
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _MARKUP_RE.sub("", text)
    # Bullet markers survive as leading "- " once the asterisks are gone.
    text = re.sub(r"(?m)^[ \t]*[-・]\s*", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()
# Bare short phrases can make Gemini TTS answer conversationally in text
# instead of speaking the text -- reproduced and worked around the same way
# in tachikoma_notifier/gemini_tts_synth.py.
_GEMINI_TTS_READ_ALOUD_PREFIX = "次のテキストをそのまま読み上げてください: "


# Said out loud, these start an enrolment: the speaker is telling the device
# who they are. Keyword-matched for the same reason the privacy cues are --
# a deterministic trigger the operator can rely on, rather than a judgement
# that might fire on "自己紹介って苦手なんだよね".
_ENROL_RE = re.compile(
    r"(自己紹介|声を覚え|声をおぼえ|私の声|俺の声|僕の声|覚えておいて.*名前|名前を覚え)")
# "俺は大輔", "私の名前は花子です", "花子といいます"
_NAME_RES = [
    re.compile(r"(?:名前は|なまえは)\s*([^\s。、！？!?]{1,12})"),
    re.compile(r"(?:私|わたし|俺|おれ|僕|ぼく)は\s*([^\s。、！？!?]{1,12})(?:です|だ|だよ|ね)?"),
    re.compile(r"([^\s。、！？!?]{1,12})\s*(?:といいます|と言います|と申します|です)"),
]
_ROLE_WORDS = [
    (people.ROLE_HOUSEHOLD, re.compile(r"(彼女|嫁|妻|奥さん|家族|同居)")),
    (people.ROLE_COLLEAGUE, re.compile(r"(同僚|会社|部下|上司|社員|仕事仲間)")),
]


# "俺の名前は大輔です" hands back "大輔です" unless the copula is peeled off,
# and the robot then addresses its owner as "大輔ですさん" forever. Longest
# first, so です is not stripped before でした.
_NAME_SUFFIX_RE = re.compile(r"(?:でした|といいます|と言います|と申します| です|です|だよ|だぜ|だな|だ|ですね|かな)$")


def _clean_name(name: str) -> str:
    previous = None
    while name and name != previous:
        previous = name
        name = _NAME_SUFFIX_RE.sub("", name).strip("　 、。,.!?！？")
    return name


def _extract_enrolment(text: str) -> Optional[tuple[str, Optional[str]]]:
    """(name, role) if this utterance is someone introducing themselves."""
    if not text or not _ENROL_RE.search(text):
        return None
    name = None
    for pattern in _NAME_RES:
        found = pattern.search(text)
        if found:
            name = _clean_name(found.group(1).strip())
            if name:
                break
            name = None
    if not name:
        return None
    role = None
    for candidate_role, pattern in _ROLE_WORDS:
        if pattern.search(text):
            role = candidate_role
            break
    return name, role


def _identify_or_enrol(device_id: str, pcm: bytes, sample_rate: int, text: str) -> None:
    """Work out who just spoke, and enrol them if they said who they are.

    Runs after transcription because the words decide whether this is an
    introduction. Never raises: failing to recognize somebody has to leave
    the conversation working, just without their standing.
    """
    if not device_id or not biometrics.voice_available():
        return
    embedding = biometrics.voice_embedding(pcm, sample_rate)
    if embedding is None:
        return

    person, score = _people_store.identify(
        "voice", embedding, people.VOICE_MATCH_THRESHOLD, people.VOICE_MATCH_MARGIN)

    enrolment = _extract_enrolment(text)
    if enrolment is not None:
        name, stated_role = enrolment
        if person is not None:
            # Already known -- treat it as another sample of the same voice
            # rather than a second person with the same name.
            _people_store.add_embedding(person["id"], "voice", embedding)
            if stated_role:
                _people_store.set_role(person["id"], stated_role)
            _log(f"gateway voice enrolment: existing person, sample added score={score:.2f}")
        else:
            # The very first person to introduce themselves is the operator.
            # There is nobody enrolled who could have authorized it, and
            # somebody has to be able to grant the rest.
            first_ever = not _people_store.list_people()
            role = people.ROLE_MASTER if first_ever else (stated_role or people.ROLE_GUEST)
            person_id = _people_store.add_person(name, role)
            _people_store.add_embedding(person_id, "voice", embedding)
            person = _people_store.get(person_id)
            _log(f"gateway voice enrolment: new person role={role} "
                 f"{'(first ever -> master)' if first_ever else ''}")

    if person is not None:
        _people_store.note_encounter(person["id"])
        # Confident matches feed the enrolment. enrolment-branch adds are
        # skipped here so an introduction does not store the same clip twice.
        if enrolment is None and score >= people.VOICE_SAMPLE_REFRESH_THRESHOLD:
            _people_store.add_embedding(person["id"], "voice", embedding)
            _log(f"gateway voice sample refreshed from confident match score={score:.2f}")
        set_current_speaker(device_id, person, score, "voice")
        # Names are personal; the log records the decision, not the person.
        _log(f"gateway speaker identified role={person.get('role')} score={score:.2f}")
    else:
        set_current_speaker(device_id, None, score, "voice")
        if score:
            _log(f"gateway speaker not identified (best={score:.2f}) -> least privilege")


def _voicevox_speaker_options() -> list[dict[str, str]]:
    """The voices actually installed, for the settings screen.

    Asks the engine rather than shipping a list, because the catalogue
    depends on which VOICEVOX build is installed. An unreachable engine
    yields an empty list, which the UI shows as "voices unavailable" instead
    of offering choices that would silently fail.
    """
    base_url = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/speakers", timeout=5) as response:
            speakers = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except Exception:
        return []
    options = []
    for speaker in speakers:
        for style in speaker.get("styles", []):
            options.append({"value": str(style["id"]),
                            "label": f"{speaker['name']} / {style['name']}"})
    return options


settings_store.register_option_provider("voicevox_speakers", _voicevox_speaker_options)


def process_settings_get(headers: dict[str, str] | None = None,
                         env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    return 200, {"schema": settings_store.schema(), "values": settings_store.load()}


def process_settings_put(payload: dict[str, Any], headers: dict[str, str] | None = None,
                         env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not isinstance(payload, dict):
        return _result(400, "invalid_input")
    values, rejected = settings_store.update(payload)
    # Names of settings only -- their values can be persona text, which is
    # the operator's writing and does not belong in a log.
    _log(f"gateway settings updated keys={sorted(k for k in payload if k not in rejected)}"
         + (f" rejected={rejected}" if rejected else ""))
    return 200, {"values": values, "rejected": rejected}


def process_people_list(headers: dict[str, str] | None = None,
                        env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """Who the device has met. Embeddings are never returned -- the app has
    no use for them, and they are the most sensitive thing in the store."""
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    listed = []
    for person in _people_store.list_people():
        listed.append({
            "id": person["id"],
            "name": person.get("name", ""),
            "role": person.get("role", people.ROLE_GUEST),
            "encounters": person.get("encounters", 0),
            "last_seen": person.get("last_seen"),
            "first_seen": person.get("first_seen"),
            "voice_samples": len(person.get("voice") or []),
            "face_samples": len(person.get("face") or []),
        })
    listed.sort(key=lambda p: p.get("last_seen") or 0, reverse=True)
    return 200, {"people": listed, "roles": list(people._ROLE_RANK.keys())}


def process_people_update(payload: dict[str, Any], headers: dict[str, str] | None = None,
                          env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    person_id = payload.get("id")
    if not isinstance(person_id, str) or not person_id:
        return _result(400, "invalid_input")
    if payload.get("delete") is True:
        if _people_store.delete_person(person_id):
            _log("gateway person deleted")
            return 200, {"ok": True}
        return _result(404, "not_found")
    role = payload.get("role")
    if isinstance(role, str) and _people_store.set_role(person_id, role):
        _log(f"gateway person role set to {role}")
        return 200, {"ok": True}
    return _result(400, "invalid_input")


def process_vision(image_bytes: bytes, headers: dict[str, str] | None = None,
                   env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """One camera frame in; where to look and who it is, out.

    Detection and recognition share the same inference, so pointing the head
    at somebody and knowing who they are cost one pass between them.

    `look_at` is normalized to -1..1 with the origin at the centre of the
    frame, which is what Motion::lookAtNormalized on the device already
    takes -- the firmware does not need to know the frame size, the lens, or
    anything about faces.
    """
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not image_bytes:
        return _result(400, "invalid_input")

    # Debug aid: keep the most recent frame on disk so the operator can see
    # what the camera actually sees. Off by default and gitignored -- a frame
    # is a photograph of whoever is in the room, which is the same personal
    # data the memory store and the logs are careful not to leak.
    if env.get("TACHIKOMA_SAVE_VISION_FRAMES") == "1":
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "last_frame.jpg"), "wb") as handle:
                handle.write(image_bytes)
        except OSError:
            pass  # A debug aid must never take the vision endpoint down.

    if not biometrics.face_available():
        return _result(503, "server_error")

    image = biometrics.decode_image(image_bytes)
    if image is None:
        return _result(400, "invalid_input")

    faces = biometrics.detect_faces(image)
    if not faces:
        return 200, {"faces": 0, "look_at": None, "person": None}

    # The largest face is the one being talked to. Somebody across the room
    # is in frame but is not the conversation.
    target = faces[0]
    result: dict[str, Any] = {
        "faces": len(faces),
        "look_at": {"x": round(target["center_x"], 4), "y": round(target["center_y"], 4)},
        "area_ratio": round(target["area_ratio"], 4),
        "person": None,
    }

    device_id = headers.get("X-Device-Id", "")
    if env.get("ENABLE_FACE_ID", "1") == "1":
        embedding = biometrics.face_embedding(image, target)
        if embedding is not None:
            person, score = _people_store.identify(
                "face", embedding, people.FACE_MATCH_THRESHOLD, people.FACE_MATCH_MARGIN)
            if person is not None:
                _people_store.note_encounter(person["id"])
                result["person"] = {"role": person.get("role"), "score": round(score, 3)}
                # A face confirms a voice; it does not outrank one. Only
                # raise standing here when nobody has been identified by
                # voice, so a photograph held up to the camera cannot
                # promote itself over whoever is actually speaking.
                if get_current_speaker(device_id) is None:
                    set_current_speaker(device_id, person, score, "face")
                _log(f"gateway face identified role={person.get('role')} score={score:.2f}")
            else:
                # Learn the face of whoever the voice already identified, so
                # the next encounter can be recognized on sight.
                speaker = get_current_speaker(device_id)
                if speaker and speaker["modality"] == "voice" and target["area_ratio"] > 0.02:
                    if _people_store.add_embedding(speaker["person_id"], "face", embedding):
                        _log("gateway face remembered for the current speaker")
    return 200, result


def _log_grounding(candidate: dict[str, Any]) -> None:
    """Record what was searched and how many sources backed the answer.

    Deliberately queries and counts only, never the answer text or page
    contents -- same rule as the rest of the access log, which never carries
    conversation content by default.
    """
    metadata = candidate.get("groundingMetadata") or {}
    queries = metadata.get("webSearchQueries") or []
    if not queries:
        return
    sources = len(metadata.get("groundingChunks") or [])
    _log(f"gateway web search queries={queries} sources={sources}")


def _gemini_chat_request(text: str, payload: dict[str, Any], env: dict[str, str],
                         key: str, *, stream: bool) -> urllib.request.Request:
    """The chat call to Gemini, streaming or not.

    The two paths differ only in which endpoint they post to. Everything that
    decides what the model is told -- the persona, the memory this particular
    listener is allowed to hear, the tools -- is the same for both, and
    deciding it in two places is how the two answers drift apart.
    """
    model = env.get("AI_PROVIDER_MODEL", GEMINI_DEFAULT_CHAT_MODEL)
    endpoint = "streamGenerateContent?alt=sse" if stream else "generateContent"
    device_id = payload["device_id"]
    suffix, prior = (_memory_prompt_parts(device_id, current_role(device_id))
                     if _memory_enabled(env) else ("", []))
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT + _persona_suffix() + suffix}]},
        "contents": prior + [{"role": "user", "parts": [{"text": text}]}],
    }
    tools = _chat_tools(env)
    if tools:
        body["tools"] = tools
    return urllib.request.Request(
        f"{GEMINI_API_BASE_URL}/models/{model}:{endpoint}",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})


def _gemini_http_failure(exc: urllib.error.HTTPError) -> tuple[int, dict[str, Any]]:
    """How an HTTP status from Gemini is reported to the device.

    Both chat paths mapped these identically. One copy means a case added
    here cannot be forgotten in the other one.
    """
    if exc.code in (401, 403):
        return _result(502, "authentication_failed")
    if exc.code == 429:
        return _result(503, "rate_limited")
    return _result(502, "server_error")


def _gemini_chat_response(text: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return _result(503, "server_error")
    request = _gemini_chat_request(text, payload, env, key, stream=False)
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_OUTPUT_BYTES * 4 + 1).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _gemini_http_failure(exc)
    except (urllib.error.URLError, TimeoutError, ValueError):
        # JSONDecodeError is a ValueError, so a truncated reply lands here too.
        return _result(504, "timeout")

    try:
        # A grounded answer can arrive split across several parts, and parts
        # carrying only groundingMetadata have no "text" at all -- taking
        # parts[0]["text"] would drop most of the reply or raise.
        parts = decoded["candidates"][0]["content"]["parts"]
        answer = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
    except (KeyError, IndexError, TypeError):
        return _result(502, "invalid_response")
    answer, reaction = _split_emotion_tag(answer) if isinstance(answer, str) else ("", None)
    set_pending_emotion(payload["device_id"], reaction)
    answer = _speakable(answer)
    if not answer or len(answer.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return _result(502, "invalid_response")
    _log_grounding(decoded.get("candidates", [{}])[0])
    return 200, {"text": answer, "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


def _voicevox_tts_pcm(text: str, env: dict[str, str]) -> Optional[bytes]:
    """Synthesize text with a local VOICEVOX engine; None (never raises) on failure.

    Gemini TTS is a round trip to Google for every sentence and measured
    2500-3484ms (mean 2958ms) on short replies -- the single largest chunk of
    the delay between the user finishing a sentence and the device answering.
    VOICEVOX runs on this machine, so synthesis is local and typically an
    order of magnitude faster.

    It is also an exact format match: VOICEVOX's default output is 24kHz
    16-bit mono WAV, and /v1/speak_queue serves audio/L16;rate=24000;channels=1,
    so only the RIFF header has to come off -- no resampling, no conversion.
    """
    base_url = env.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
    speaker = env.get("VOICEVOX_SPEAKER", "3")
    # 8s, not 20s. A sentence takes ~700ms warm, so anything approaching this
    # is a stuck engine, and the whole point of the local path is speed --
    # waiting 20s before falling back to Gemini is far worse than the 2.7s
    # Gemini would have taken outright. Seen live: one stalled sentence
    # turned a 4s reply into a 24s one.
    timeout = float(env.get("VOICEVOX_TIMEOUT_SECONDS", "8"))
    try:
        query_url = f"{base_url}/audio_query?{urllib.parse.urlencode({'text': text, 'speaker': speaker})}"
        request = urllib.request.Request(query_url, data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            query = json.loads(response.read(1024 * 1024).decode("utf-8"))
        # SpeechAnnouncer expects exactly 24kHz mono; do not let a preset or a
        # future engine default silently change either one.
        query["outputSamplingRate"] = 24000
        query["outputStereo"] = False
        # The speaking-rate setting existed in the app but was never sent, so
        # moving the slider did nothing. It also matters for latency: TTS is
        # the largest per-sentence cost and scales with how long the audio
        # is, so a faster voice is a faster reply as well as a quicker one.
        try:
            speed = float(settings_store.get("voice_speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        query["speedScale"] = max(0.5, min(2.0, speed))

        synth_url = f"{base_url}/synthesis?{urllib.parse.urlencode({'speaker': speaker})}"
        request = urllib.request.Request(synth_url, data=json.dumps(query).encode("utf-8"),
                                         method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            wav = response.read(MAX_SPEECH_AUDIO_BYTES * 2)
    except Exception:
        # Same contract as _gemini_tts_pcm: a TTS failure must never break the
        # chat response, the caller falls back to the confirmation tone.
        return None

    # Strip the RIFF container: find the "data" chunk rather than assuming the
    # canonical 44-byte header, since engines may emit extra chunks (LIST/fact).
    if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return None
    offset = 12
    while offset + 8 <= len(wav):
        chunk_id = wav[offset:offset + 4]
        chunk_size = int.from_bytes(wav[offset + 4:offset + 8], "little")
        if chunk_id == b"data":
            pcm = wav[offset + 8:offset + 8 + chunk_size]
            return pcm or None
        offset += 8 + chunk_size + (chunk_size & 1)
    return None


def _tts_pcm(text: str, env: dict[str, str]) -> Optional[bytes]:
    """Synthesize via whichever TTS provider is configured.

    Defaults to voicevox when TTS_PROVIDER is unset only if a VOICEVOX_URL was
    given explicitly; otherwise stays on the previous gemini behavior so an
    existing deployment does not change provider by upgrading this file.

    Returns None when the operator has turned speech off, which the callers
    already treat as "no audio for this sentence". The reply is still
    generated, remembered and returned as text -- it simply is not spoken,
    and a sentence nobody will hear costs no synthesis.
    """
    if not settings_store.get("speech_enabled"):
        return None
    provider = env.get("TTS_PROVIDER", "").lower()
    if not provider:
        provider = "voicevox" if env.get("VOICEVOX_URL") else "gemini"
    if provider == "voicevox":
        pcm = _voicevox_tts_pcm(text, env)
        if pcm is not None:
            return pcm
        if env.get("TTS_FALLBACK_TO_GEMINI", "1") != "1":
            return None
        _log("gateway voicevox TTS failed; falling back to gemini for this sentence")
    return _gemini_tts_pcm(text, env)


def _gemini_tts_pcm(text: str, env: dict[str, str]) -> Optional[bytes]:
    """Synthesize text via Gemini native TTS; returns None (never raises) on any failure.

    Mirrors tachikoma_notifier/gemini_tts_synth.py's verified request/response
    shape: responseModalities=["AUDIO"], response audio is raw 16-bit PCM at
    24000Hz (audio/L16;codec=pcm;rate=24000, base64-encoded) -- matching what
    /v1/speak_queue serves (audio/L16;rate=24000;channels=1) exactly, so no
    resampling is needed.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return None
    model = env.get("GEMINI_TTS_MODEL", GEMINI_DEFAULT_TTS_MODEL)
    voice = env.get("GEMINI_TTS_VOICE", GEMINI_DEFAULT_TTS_VOICE)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    request_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": _GEMINI_TTS_READ_ALOUD_PREFIX + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_SPEECH_AUDIO_BYTES * 2).decode("utf-8"))
        inline_data = decoded["candidates"][0]["content"]["parts"][0]["inlineData"]
        mime_type = inline_data["mimeType"]
        if "L16" not in mime_type or "rate=24000" not in mime_type:
            return None
        pcm = base64.b64decode(inline_data["data"], validate=True)
        return pcm or None
    except Exception:
        # Never let a TTS failure break the chat response itself -- the
        # caller falls back to the confirmation tone. Deliberately broad:
        # network errors, malformed JSON, missing keys, and bad base64 are
        # all equally "no audio this time", not a /v1/chat failure.
        return None


GEMINI_SENTENCE_DELIMITERS = "。！？!?"
GEMINI_MIN_SENTENCE_CHARS = 3


def _gemini_streaming_enabled(env: dict[str, str]) -> bool:
    return env.get("GEMINI_STREAMING", "1") != "0"


def _extract_ready_sentences(pending: str) -> tuple[list[str], str]:
    """Splits pending into zero or more sentences ready to speak, plus the
    remaining unflushed tail (kept for the next call, or for a final
    end-of-stream flush by the caller).

    A candidate ending at a delimiter is held back -- merged into whatever
    follows -- while it is at most GEMINI_MIN_SENTENCE_CHARS characters
    (stripped), so a fragment like "はい。" gets combined with the next
    sentence instead of triggering its own (wasted) TTS call. Pure and
    network-free so the splitting logic is unit-testable on its own.
    """
    sentences: list[str] = []
    start = 0
    for i, ch in enumerate(pending):
        if ch not in GEMINI_SENTENCE_DELIMITERS:
            continue
        candidate = pending[start:i + 1]
        if len(candidate.strip()) > GEMINI_MIN_SENTENCE_CHARS:
            sentences.append(candidate)
            start = i + 1
        # else: too short on its own -- left in place so it merges into
        # whatever candidate is found at the next delimiter.
    return sentences, pending[start:]


def _iter_gemini_sse_text_deltas(response: Any):
    """Yields each incremental text delta from a streamGenerateContent SSE
    response, in arrival order. Lines that aren't a well-formed `data: {...}`
    event, or don't carry a text part (e.g. a bare finishReason chunk), are
    silently skipped -- a stream is expected to contain a mix of these.
    """
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        chunk_str = line[len("data:"):].strip()
        if not chunk_str or chunk_str == "[DONE]":
            continue
        try:
            chunk = json.loads(chunk_str)
            candidate = chunk["candidates"][0]
            # Join every text part, not parts[0]: a grounded chunk can carry
            # several, and the grounding metadata rides in parts without any
            # "text" key at all.
            parts = candidate["content"]["parts"]
            delta = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(chunk, dict):
            try:
                _log_grounding(chunk["candidates"][0])
            except (KeyError, IndexError, TypeError):
                pass
        if isinstance(delta, str) and delta:
            yield delta


class _SentenceSpeaker:
    """Synthesises and queues each sentence as soon as the reply yields one.

    One sentence failing is not fatal: it is logged and skipped so the ones
    behind it still get spoken. What the caller needs afterwards is only how
    many were actually queued -- zero means nothing was heard at all, which is
    the case that has to fall back to the confirmation tone.
    """

    def __init__(self, device_id: str, env: dict[str, str], started: float) -> None:
        self._device_id = device_id
        self._env = env
        self._started = started
        self.enqueued = 0
        self._index = 0

    def speak(self, sentence: str) -> None:
        # Strip markup before synthesis, not after: TTS pronounces a stray
        # "**" or a URL literally. See _speakable().
        sentence = _speakable(sentence)
        if not sentence:
            return
        self._index += 1
        idx = self._index
        t_ready = time.monotonic()
        pcm = _tts_pcm(sentence, self._env)
        t_tts = time.monotonic()
        if pcm is None:
            _log(f"gateway streaming sentence={idx} tts_failed text_len={len(sentence)} "
                 f"sentence_ready_ms={(t_ready - self._started) * 1000:.0f} "
                 f"tts_ms={(t_tts - t_ready) * 1000:.0f}")
            return
        # append only after the first: the first one replaces whatever the
        # previous turn left pending, the rest queue behind it in speaking order.
        status, body = enqueue_speech(self._device_id, pcm, append=self.enqueued > 0)
        t_enqueue = time.monotonic()
        if status != 200:
            _log(f"gateway streaming sentence={idx} enqueue_failed status={status} "
                 f"error={body.get('error')} pcm_bytes={len(pcm)}")
            return
        self.enqueued += 1
        _log(f"gateway streaming sentence={idx} text_len={len(sentence)} pcm_bytes={len(pcm)} "
             f"sentence_ready_ms={(t_ready - self._started) * 1000:.0f} "
             f"tts_ms={(t_tts - t_ready) * 1000:.0f} "
             f"enqueue_ms={(t_enqueue - t_tts) * 1000:.0f} "
             f"total_ms={(t_enqueue - self._started) * 1000:.0f}")


def _resolve_emotion_tag(device_id: str, pending: str,
                         started: float) -> tuple[str, bool]:
    """Take the emotion tag off the front of a reply, once it can be read.

    The tag sits at the very start, so it resolves from the first chunk --
    before any sentence has been synthesized. Waiting for a sentence boundary
    would put the motion behind the voice.

    Stripping happens whether or not emotions are enabled: the model is still
    asked for the tag, and a disabled setting must not turn into the device
    saying the word "happy" out loud. Only acting on it is optional.

    Returns the text with the tag removed, and whether the question is settled.
    A partial "[hap" is not settled, and must not be read as "no tag".
    """
    stripped, reaction = _split_emotion_tag(pending)
    if reaction is not None and settings_store.get("emotion_enabled"):
        set_pending_emotion(device_id, reaction)
        _log(f"gateway emotion={reaction} "
             f"resolved_ms={(time.monotonic() - started) * 1000:.0f}")
    if reaction is None and _EMOTION_TAG_MAYBE_RE.match(pending):
        return pending, False
    return stripped, True


def _gemini_stream_chat_and_speak(text: str, payload: dict[str, Any],
                                  env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Streaming counterpart to _gemini_chat_response() + the TTS/enqueue
    block in process_chat(): as Gemini's reply streams in, each completed
    sentence is synthesized and enqueued immediately (in speaking order,
    via enqueue_speech(..., append=True)) instead of waiting for the full
    reply before any audio exists at all. Returns the same (status, body)
    shape as the non-streaming path, so process_chat() doesn't need to know
    which one ran.

    A sentence's TTS failure is logged and skipped, not fatal -- later
    sentences still get their turn. Only a total failure (no sentence ever
    enqueued) falls back to the fixed confirmation tone, matching the
    non-streaming path's existing behavior.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return _result(503, "server_error")
    device_id = payload["device_id"]
    request = _gemini_chat_request(text, payload, env, key, stream=True)

    t_start = time.monotonic()
    full_text_parts: list[str] = []
    speaker = _SentenceSpeaker(device_id, env, t_start)

    pending = ""
    first_chunk_logged = False
    emotion_resolved = False
    set_pending_emotion(device_id, None)  # clear anything left from a previous turn
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            for delta in _iter_gemini_sse_text_deltas(response):
                if not first_chunk_logged:
                    first_chunk_logged = True
                    _log(f"gateway streaming first_chunk_ms={(time.monotonic() - t_start) * 1000:.0f}")
                full_text_parts.append(delta)
                pending += delta
                if not emotion_resolved:
                    pending, emotion_resolved = _resolve_emotion_tag(device_id, pending, t_start)
                    if emotion_resolved:
                        # The deltas gathered so far still carry the tag; the
                        # stripped text replaces them so the reply the device
                        # is sent matches what it was given to say.
                        full_text_parts[:] = [pending]
                ready, pending = _extract_ready_sentences(pending)
                for sentence in ready:
                    speaker.speak(sentence)
    except urllib.error.HTTPError as exc:
        return _gemini_http_failure(exc)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return _result(504, "timeout")

    if pending.strip():
        speaker.speak(pending)
    enqueued_count = speaker.enqueued

    # Same cleanup the spoken sentences got, so the text the device receives
    # matches what it just said instead of carrying leftover markup.
    full_text = _speakable("".join(full_text_parts))
    if not full_text:
        return _result(502, "invalid_response")
    full_text = full_text[:MAX_OUTPUT_BYTES]

    if enqueued_count == 0 and settings_store.get("speech_enabled"):
        # Every sentence's TTS (or the stream itself) failed -- same
        # fallback the non-streaming path uses so /v1/chat still produces
        # *something* audible rather than silence with no explanation.
        #
        # Not when speech is switched off, though: there the silence is the
        # point, and a confirmation beep is the one sound guaranteed to
        # annoy someone who just asked for quiet.
        enqueue_speech(device_id, _generate_beep_pcm())

    return 200, {"text": full_text, "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


def _provider_response(text: str, payload: dict[str, Any], env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    provider = env.get("AI_PROVIDER", "mock").lower()
    if provider == "mock":
        answer = env.get("MOCK_RESPONSE", "こんにちは。タチコマ接続テストは成功です。")
        return 200, {"text": answer[:MAX_OUTPUT_BYTES], "request_id": payload["request_id"],
                     "session_id": payload["session_id"], "is_final": True}
    if provider == "gemini":
        return _gemini_chat_response(text, payload, env)

    url = env.get("AI_PROVIDER_URL", "")
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not url or not key:
        return _result(503, "server_error")
    if not url.startswith("https://") and env.get("ALLOW_INSECURE_DEV") != "1":
        return _result(503, "server_error")
    request_body = json.dumps({
        "model": env.get("AI_PROVIDER_MODEL", "default"),
        "messages": [{"role": "user", "content": text}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
    })
    try:
        context = ssl.create_default_context() if url.startswith("https://") else None
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=context) as response:
            decoded = json.loads(response.read(MAX_OUTPUT_BYTES + 1).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _result(504, "timeout")

    try:
        answer = decoded["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _result(502, "invalid_response")
    if not isinstance(answer, str) or not answer or len(answer.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return _result(502, "invalid_response")
    return 200, {"text": answer, "request_id": payload["request_id"],
                 "session_id": payload["session_id"], "is_final": True}


def _generate_beep_pcm(*, duration_s: float = 0.4, freq_hz: float = 880.0, sample_rate: int = 24000) -> bytes:
    """A fixed confirmation tone -- NOT TTS. Proves the chat->speak_queue
    wiring end-to-end without a text-to-speech provider (tracked separately
    alongside the Gemini+VOICEVOX pipeline work). 16-bit mono PCM at
    sample_rate, matching what SpeechAnnouncer expects from /v1/speak_queue.
    """
    n_samples = int(duration_s * sample_rate)
    samples = bytearray()
    for i in range(n_samples):
        value = int(8000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        samples += struct.pack("<h", value)
    return bytes(samples)


def process_chat(payload: dict[str, Any], headers: dict[str, str] | None = None,
                 env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not all(isinstance(payload.get(key), str) and payload[key] for key in ("request_id", "session_id", "device_id")):
        return _result(400, "invalid_input")
    text = payload.get("text")
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        return _result(400, "invalid_input")

    command = detect_manner_command(text)
    if command:
        set_pending_command(payload["device_id"], command)
        _log(f"gateway manner command={command}")

    # Grave lookups (R6): a recognized CRM question never reaches Gemini and
    # is never remembered -- the reply is spoken, returned, and forgotten.
    # The CRM's own audit log is the record of who asked.
    speaker = get_current_speaker(payload["device_id"])
    crm_reply = crm_bridge.intercept(text, speaker["name"] if speaker else "unknown",
                                     device_id=payload["device_id"])
    if crm_reply is not None:
        if settings_store.get("speech_enabled"):
            pcm = _tts_pcm(crm_reply, env) or _generate_beep_pcm()
            enqueue_status, enqueue_body = enqueue_speech(payload["device_id"], pcm)
            if enqueue_status != 200:
                _log(f"gateway crm_bridge enqueue failed status={enqueue_status} "
                     f"error={enqueue_body.get('error')}")
        return 200, {"text": crm_reply, "request_id": payload["request_id"],
                     "session_id": payload["session_id"], "is_final": True}

    # Mobile order (FR-1/FR-5): checked before the LLM so an order utterance
    # or an approval answer never becomes small talk. order_bridge fails
    # soft -- agent down means None, and chat continues untouched.
    order_reply = order_bridge.intercept(text, payload["device_id"], get_last_location())
    if order_reply is not None:
        _log(f"gateway order_bridge reply_len={len(order_reply)}")
        if settings_store.get("speech_enabled"):
            pcm = _tts_pcm(order_reply, env) or _generate_beep_pcm()
            enqueue_status, enqueue_body = enqueue_speech(payload["device_id"], pcm)
            if enqueue_status != 200:
                _log(f"gateway order_bridge enqueue failed status={enqueue_status} "
                     f"error={enqueue_body.get('error')}")
        _remember_exchange(payload["device_id"], text, order_reply, env)
        return 200, {"text": order_reply, "request_id": payload["request_id"],
                     "session_id": payload["session_id"], "is_final": True}

    provider = env.get("AI_PROVIDER", "mock").lower()
    if provider == "gemini" and _gemini_streaming_enabled(env):
        # Owns TTS/enqueue itself (per completed sentence, as they arrive)
        # instead of the single after-the-fact block below -- see
        # _gemini_stream_chat_and_speak's docstring.
        status, body = _gemini_stream_chat_and_speak(text, payload, env)
        if status == 200:
            _remember_exchange(payload["device_id"], text, body.get("text", ""), env)
        return status, body

    status, body = _provider_response(text, payload, env)
    if status == 200:
        pcm = None
        if provider == "gemini":
            pcm = _tts_pcm(body["text"], env)
        # Speech switched off means say nothing at all -- not even the
        # fallback tone. The reply is still returned and still remembered,
        # so only the enqueue below is skipped, never the remembering.
        speaking = bool(settings_store.get("speech_enabled"))
        if pcm is None and speaking:
            # No real TTS (non-Gemini provider, or Gemini TTS failed this
            # time): enqueue a fixed tone instead, solely to verify the
            # transcribe->chat->speak_queue path is wired end-to-end. A TTS
            # failure never fails the /v1/chat response itself.
            pcm = _generate_beep_pcm()
        enqueue_status, enqueue_body = (200, {}) if pcm is None else enqueue_speech(
            payload["device_id"], pcm)
        if enqueue_status != 200:
            # Previously silent: enqueue_speech()'s return value was
            # discarded here, so a rejection (e.g. 413 for audio over
            # MAX_SPEECH_AUDIO_BYTES) left /v1/chat looking like a full
            # success -- text delivered, but the device would never hear
            # anything, with no log line anywhere explaining why.
            _log(f"gateway enqueue_speech failed status={enqueue_status} "
                 f"error={enqueue_body.get('error')} pcm_bytes={len(pcm)} "
                 f"device_id={payload['device_id']}")
        _remember_exchange(payload["device_id"], text, body.get("text", ""), env)
    return status, body


def _pcm_to_wav(pcm: bytes, sample_rate: int, *, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Wraps headerless 16-bit PCM in a minimal WAV container.

    Cloud STT providers (e.g. OpenAI's /v1/audio/transcriptions) expect a
    real audio file, not a bare sample buffer, so the device's raw upload
    must be wrapped before it is forwarded.
    """
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _build_multipart_body(boundary: str, wav_bytes: bytes, filename: str, model: str) -> bytes:
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model.encode("utf-8") + b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8"),
        wav_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts)


def _gemini_stt_text(pcm: bytes, sample_rate: int, env: dict[str, str]) -> Optional[str]:
    """Transcribe via Gemini's audio-understanding input; returns None (never
    raises) on any failure. Reuses AI_PROVIDER_API_KEY -- the same Gemini
    project/key already configured for chat and TTS, not a separate
    STT-specific credential. Wraps the device's raw PCM in a WAV container
    (_pcm_to_wav, already used for the generic OpenAI-compatible path below)
    and sends it as inlineData alongside a "transcribe verbatim" instruction,
    the same read-aloud-style workaround _gemini_tts_pcm uses in reverse.
    """
    key = env.get("AI_PROVIDER_API_KEY", "")
    if not key:
        return None
    model = env.get("GEMINI_STT_MODEL", GEMINI_DEFAULT_STT_MODEL)
    url = f"{GEMINI_API_BASE_URL}/models/{model}:generateContent"
    wav_b64 = base64.b64encode(_pcm_to_wav(pcm, sample_rate)).decode("ascii")
    request_body = json.dumps({
        "contents": [{"role": "user", "parts": [
            {"text": _stt_prompt(env)},
            {"inlineData": {"mimeType": "audio/wav", "data": wav_b64}},
        ]}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key,
    })
    try:
        with urllib.request.urlopen(request, timeout=float(env.get("AI_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=ssl.create_default_context()) as response:
            decoded = json.loads(response.read(MAX_INPUT_BYTES * 4 + 1024).decode("utf-8"))
        text = decoded["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip() if isinstance(text, str) else ""
        return text or None
    except Exception:
        return None


def _stt_response(pcm: bytes, sample_rate: int, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    provider = env.get("STT_PROVIDER", "mock").lower()
    if provider == "mock":
        text = env.get("MOCK_TRANSCRIPTION", "こんにちは")
        return 200, {"text": text[:MAX_INPUT_BYTES]}
    if provider == "gemini":
        text = _gemini_stt_text(pcm, sample_rate, env)
        if text is None:
            return _result(502, "invalid_response")
        return 200, {"text": text[:MAX_INPUT_BYTES]}

    url = env.get("STT_PROVIDER_URL", "")
    key = env.get("STT_PROVIDER_API_KEY", "")
    if not url or not key:
        return _result(503, "server_error")
    if not url.startswith("https://") and env.get("ALLOW_INSECURE_DEV") != "1":
        return _result(503, "server_error")

    wav_bytes = _pcm_to_wav(pcm, sample_rate)
    boundary = uuid.uuid4().hex
    body = _build_multipart_body(boundary, wav_bytes, "audio.wav", env.get("STT_PROVIDER_MODEL", "whisper-1"))
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {key}",
    })
    try:
        context = ssl.create_default_context() if url.startswith("https://") else None
        with urllib.request.urlopen(request, timeout=float(env.get("STT_PROVIDER_TIMEOUT_SECONDS", "30")),
                                    context=context) as response:
            decoded = json.loads(response.read(MAX_INPUT_BYTES + 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(502, "authentication_failed")
        if exc.code == 429:
            return _result(503, "rate_limited")
        return _result(502, "server_error")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return _result(504, "timeout")

    text = decoded.get("text") if isinstance(decoded, dict) else None
    if not isinstance(text, str) or not text.strip():
        return _result(502, "invalid_response")
    return 200, {"text": text.strip()[:MAX_INPUT_BYTES]}


def process_transcribe(audio: bytes, headers: dict[str, str] | None = None, env: dict[str, str] | None = None,
                       *, sample_rate: int = 16000) -> tuple[int, dict[str, Any]]:
    headers = headers or {}
    env = env or os.environ
    if not _authorized(headers, env):
        return _result(401, "authentication_failed")
    if not isinstance(audio, (bytes, bytearray)) or not audio or len(audio) % 2 != 0:
        return _result(400, "invalid_input")
    if len(audio) > MAX_TRANSCRIBE_AUDIO_BYTES:
        return _result(413, "invalid_input")
    if not isinstance(sample_rate, int) or not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
        return _result(400, "invalid_input")

    debug = _debug_logging_enabled(env)  # TEMPORARY, see DEBUG_AUDIO_DIR block near the top of this file
    if debug:
        try:
            os.makedirs(DEBUG_AUDIO_DIR, exist_ok=True)
            filename = f"transcribe_{datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')}.wav"
            path = os.path.join(DEBUG_AUDIO_DIR, filename)
            with open(path, "wb") as f:
                f.write(_pcm_to_wav(bytes(audio), sample_rate))
            _log(f"gateway debug saved uploaded audio to {path} (sample_rate={sample_rate} "
                 f"bytes={len(audio)} duration_s={len(audio) / 2 / sample_rate:.2f})")
        except OSError as exc:
            _log(f"gateway debug failed to save uploaded audio: {type(exc).__name__}")

    duration_s = len(audio) / 2 / sample_rate
    if duration_s < MIN_TRANSCRIBE_AUDIO_SECONDS:
        # Too little audio to plausibly contain speech: skip the STT call
        # entirely rather than risk a confidently-fabricated transcription.
        # {"text": ""} is not a special case for the device -- it already
        # rejects an empty "text" field as InvalidResponse
        # (VoiceInputController::UploadAndTranscribe), the same failure path
        # a real STT error takes, so no firmware change is needed for this.
        if debug:
            _log(f"gateway debug transcribe skipped: duration_s={duration_s:.2f} "
                 f"< {MIN_TRANSCRIBE_AUDIO_SECONDS}s minimum")
        return 200, {"text": ""}

    t_stt_start = time.monotonic()
    status, body = _stt_response(bytes(audio), sample_rate, env)
    stt_ms = (time.monotonic() - t_stt_start) * 1000
    # Timing always, content only when debugging. STT is the largest single
    # component of the wait and it scales with how long the clip is, so
    # "why does it feel slower today" is answerable from the log rather than
    # from guesswork -- but the words themselves stay out of it.
    _log(f"gateway transcribe status={status} audio_s={duration_s:.1f} stt_ms={stt_ms:.0f}")
    if debug:
        detail = repr(body.get("text")) if status == 200 else body.get("error")
        _log(f"gateway debug transcribe text={detail}")

    if status == 200 and _speaker_id_enabled(env):
        # Synchronous on purpose. The chat request that decides what memory
        # to load is a separate round trip that follows immediately, so
        # doing this in the background would race it -- and losing that race
        # means the operator is treated as a stranger and cannot see his own
        # memory. Measured at 11-15ms for a 2-4s clip against ~1900ms of
        # STT in the same call, so there is nothing to gain by deferring it.
        #
        # That 11-15ms is the *warm* cost. The first call also loads the
        # encoder, which took 12.8s here, and being inside this request is
        # what made the first conversation after every restart look like a
        # hang. _warm_biometrics() now pays that at boot; if this ever feels
        # slow again, check the log for the "biometrics warmed" line before
        # suspecting the microphone.
        _identify_or_enrol(headers.get("X-Device-Id", ""), bytes(audio), sample_rate,
                           body.get("text", ""))
    return status, body


class GatewayHandler(BaseHTTPRequestHandler):
    # The G2 app runs in the Even App's WebView, served from a different
    # origin (the Vite dev server, later the packaged app), so its calls to
    # /v1/* are cross-origin and the browser demands CORS. "*" is safe here
    # because nothing is cookie-authenticated: every consequential endpoint
    # requires the bearer token, which a hostile page does not have, and the
    # server is reachable only from the LAN and the tailnet.
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_audio(self, audio: bytes, emotion: Optional[str] = None,
                    command: Optional[str] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16;rate=24000;channels=1")
        # Rides with the reply that acknowledges it, so "manner mode please"
        # and the device going still happen at the same moment.
        if command:
            self.send_header("X-Tachikoma-Command", command)
        # Rides along with the audio it belongs to, so the device starts the
        # motion at the same moment it starts speaking. Sending it on the
        # /v1/chat response instead would arrive a turn too late: the device
        # only sees that after the whole reply has been generated.
        if emotion:
            self.send_header("X-Tachikoma-Emotion", emotion)
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def _send_raw(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # The settings app is served from the same origin it calls, so no
        # caching headers are needed beyond keeping the phone from pinning a
        # stale copy of the page after an update.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str) -> None:
        self._send_raw(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _read_json(self) -> dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), MAX_INPUT_BYTES * 4)
            if length <= 0:
                return {}
            decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send_no_content(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_bounded_body(self, limit: int) -> Optional[bytes]:
        """The uploaded bytes, or None if the request does not describe a body.

        Every upload endpoint used to carry its own copy of this: read
        Content-Length, cap it, and treat a missing or unparsable one as a bad
        request. The cap is the endpoint's own limit plus slack rather than the
        limit itself, so the size check that actually rejects still belongs to
        the endpoint, which knows which error to report.

        Always read the body before rejecting anything else about the request:
        leaving it unread desynchronises a kept-alive connection, and the next
        request on it is then parsed out of the middle of this one's payload.
        """
        try:
            length = min(int(self.headers.get("Content-Length", "0")), limit + 1024)
        except ValueError:
            return None
        if length <= 0:
            return None
        return self.rfile.read(length)

    # ---- routes -------------------------------------------------------
    #
    # One method per endpoint, gathered into the tables at the end of the
    # class. The tables are the point: what this server answers used to be
    # spread through two if/elif chains deep enough that reading them meant
    # tracking which branch you were in.

    def _get_ui(self, parsed: urllib.parse.SplitResult) -> None:
        self._send_html(webui.INDEX_HTML)

    # The built G2 app, served by the same always-on process that answers it.
    # A sideloaded Even Hub app is fetched from its URL at every launch, so
    # whatever serves it decides when the glasses work; pointing the QR at the
    # Vite dev window meant Tachikoma vanished from the lenses whenever that
    # window closed. The gateway is already the thing that must be running for
    # a conversation to exist at all, so it serves the shell too.
    _G2_DIST = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "glasses", "dist"))
    _G2_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml",
                 ".json": "application/json; charset=utf-8"}

    def _get_g2_config(self, parsed: urllib.parse.SplitResult) -> None:
        # Hands the packaged G2 app its bearer token at launch, so the .ehpk
        # uploaded to Even's portal carries no credentials. Unauthenticated by
        # necessity (the caller is asking BECAUSE it has no token yet) and
        # defended by the network perimeter instead: this server is reachable
        # only from the LAN and the tailnet, the same boundary that already
        # protects the served bundle. Per-entrance tokens with real pairing
        # are R7's job.
        self._send(200, {"token": os.environ.get("DEVICE_TOKEN", "")})

    def _get_g2(self, parsed: urllib.parse.SplitResult) -> None:
        if parsed.path == "/g2/config":
            self._get_g2_config(parsed)
            return
        relative = parsed.path[len("/g2"):].lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(self._G2_DIST, relative))
        # normpath then prefix-check: the one defence that matters for a
        # path taken from the request line.
        if not target.startswith(self._G2_DIST) or not os.path.isfile(target):
            self._send(404, {"error": "not_found"})
            return
        content_type = self._G2_TYPES.get(os.path.splitext(target)[1].lower(),
                                          "application/octet-stream")
        with open(target, "rb") as handle:
            self._send_raw(200, content_type, handle.read())

    def _get_talk(self, parsed: urllib.parse.SplitResult) -> None:
        # The G2 entrance, one shell early: the same mic -> transcribe -> chat
        # -> text loop the Even Hub app will run, served as a phone page first
        # so the pipeline is proven before the BLE shell goes around it. The
        # page itself is public like /ui; every API call it makes carries the
        # bearer token the user pastes in once.
        self._send_html(webui.TALK_HTML)

    def _get_ui_manifest(self, parsed: urllib.parse.SplitResult) -> None:
        self._send_raw(200, "application/manifest+json", webui.MANIFEST_JSON.encode("utf-8"))

    def _get_ui_icon(self, parsed: urllib.parse.SplitResult) -> None:
        self._send_raw(200, "image/png", _APP_ICON_PNG)

    def _get_settings(self, parsed: urllib.parse.SplitResult) -> None:
        self._send(*process_settings_get(dict(self.headers), os.environ))

    def _get_people(self, parsed: urllib.parse.SplitResult) -> None:
        self._send(*process_people_list(dict(self.headers), os.environ))

    def _get_health(self, parsed: urllib.parse.SplitResult) -> None:
        self._send(200, {"ok": True, "provider": os.environ.get("AI_PROVIDER", "mock")})

    def _get_speak_queue(self, parsed: urllib.parse.SplitResult) -> None:
        if not _authorized(dict(self.headers), os.environ):
            self._send(401, {"error": "authentication_failed"})
            return
        device_id = urllib.parse.parse_qs(parsed.query).get("device_id", [""])[0]
        if not device_id:
            self._send(400, {"error": "invalid_input"})
            return
        audio = dequeue_speech(device_id)
        if audio is None:
            self._send_no_content()
            return
        self._send_audio(audio, take_pending_emotion(device_id),
                         take_pending_command(device_id))

    def _put_settings(self, parsed: urllib.parse.SplitResult) -> None:
        self._send(*process_settings_put(self._read_json(), dict(self.headers), os.environ))

    def _post_chat(self, parsed: urllib.parse.SplitResult) -> None:
        raw = self._read_bounded_body(MAX_INPUT_BYTES)
        try:
            payload = json.loads((raw or b"").decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "invalid_input"})
            return
        if not isinstance(payload, dict):
            # A bare list or string is valid JSON, so it got past the decode
            # and then hit .get() inside process_chat: AttributeError, no
            # response written, connection dropped. From the device that is
            # indistinguishable from the gateway having died, which is the
            # one failure it is worst at diagnosing.
            self._send(400, {"error": "invalid_input"})
            return
        self._send(*process_chat(payload, dict(self.headers), os.environ))

    def _post_people(self, parsed: urllib.parse.SplitResult) -> None:
        self._send(*process_people_update(self._read_json(), dict(self.headers), os.environ))

    def _post_vision(self, parsed: urllib.parse.SplitResult) -> None:
        image_bytes = self._read_bounded_body(MAX_VISION_IMAGE_BYTES)
        if image_bytes is None:
            self._send(400, {"error": "invalid_input"})
            return
        self._send(*process_vision(image_bytes, dict(self.headers), os.environ))

    def _post_speak(self, parsed: urllib.parse.SplitResult) -> None:
        # Read before judging. Answering 401 while the upload is still in
        # flight leaves the body unread, and the client sometimes sees the
        # connection reset instead of the status -- observed once in twenty
        # attempts, on the old code as well. The bytes are bounded and then
        # discarded, so an unauthorised caller gains nothing by sending them.
        device_id = self.headers.get("X-Device-Id", "")
        audio = self._read_bounded_body(MAX_SPEECH_AUDIO_BYTES)
        if not _authorized(dict(self.headers), os.environ):
            self._send(401, {"error": "authentication_failed"})
            return
        if audio is None:
            self._send(400, {"error": "invalid_input"})
            return
        self._send(*enqueue_speech(device_id, audio))

    def _post_location(self, parsed: urllib.parse.SplitResult) -> None:
        # An iPhone shortcut posts {"lat": .., "lng": ..} here (FR-2). Same
        # bearer token as every other endpoint; the tailnet is the transport.
        body = self._read_bounded_body(4096)
        if not _authorized(dict(self.headers), os.environ):
            self._send(401, {"error": "authentication_failed"})
            return
        try:
            payload = json.loads(body or b"{}")
            lat = float(payload["lat"])
            lng = float(payload["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_input"})
            return
        set_last_location(lat, lng)
        _log(f"gateway location updated lat={lat:.4f} lng={lng:.4f}")
        self._send(200, {"ok": True})

    def _get_location(self, parsed: urllib.parse.SplitResult) -> None:
        if not _authorized(dict(self.headers), os.environ):
            self._send(401, {"error": "authentication_failed"})
            return
        self._send(200, {"location": get_last_location()})

    def _post_announce(self, parsed: urllib.parse.SplitResult) -> None:
        # Text-to-announcement: TTS + enqueue for the device, so local
        # processes (order agent, approval daemon) can make the robot speak
        # without carrying their own TTS credentials. /v1/speak stays the
        # raw-PCM sibling for callers that already have audio.
        body = self._read_bounded_body(8192)
        if not _authorized(dict(self.headers), os.environ):
            self._send(401, {"error": "authentication_failed"})
            return
        try:
            payload = json.loads(body or b"{}")
            device_id = payload["device_id"]
            text = payload["text"]
            if not isinstance(device_id, str) or not device_id \
                    or not isinstance(text, str) or not text.strip():
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_input"})
            return
        # Sentence by sentence, like the streaming chat path: one PCM blob
        # for a long announcement exceeds MAX_SPEECH_AUDIO_BYTES (observed
        # 413 on the first real order readback), while per-sentence chunks
        # queue behind each other via append and play in order.
        sentences = [s for s in re.split(r"(?<=[。！？!?])", text[:1000]) if s.strip()]
        enqueued = 0
        for sentence in sentences:
            pcm = _tts_pcm(sentence, os.environ)
            if pcm is None:
                continue
            status, body = enqueue_speech(device_id, pcm, append=enqueued > 0)
            if status == 200:
                enqueued += 1
        if enqueued == 0:
            self._send(502, {"error": "tts_failed"})
            return
        self._send(200, {"ok": True, "sentences": enqueued})

    def _post_transcribe(self, parsed: urllib.parse.SplitResult) -> None:
        debug = _debug_logging_enabled(os.environ)  # TEMPORARY, see DEBUG_AUDIO_DIR block
        t_upload_start = time.monotonic() if debug else None
        audio = self._read_bounded_body(MAX_TRANSCRIBE_AUDIO_BYTES)
        try:
            sample_rate: Optional[int] = int(self.headers.get("X-Sample-Rate", "16000"))
        except ValueError:
            sample_rate = None
        if audio is None or sample_rate is None:
            self._send(400, {"error": "invalid_input"})
            return
        if debug:
            _log(f"gateway debug upload_ms={(time.monotonic() - t_upload_start) * 1000:.0f} "
                 f"bytes={len(audio)} sample_rate={sample_rate}")
        self._send(*process_transcribe(audio, dict(self.headers), os.environ,
                                       sample_rate=sample_rate))

    _GET_ROUTES = {
        "/": _get_ui,
        "/ui": _get_ui,
        "/ui/": _get_ui,
        "/talk": _get_talk,
        "/ui/manifest.json": _get_ui_manifest,
        "/ui/icon.png": _get_ui_icon,
        "/v1/settings": _get_settings,
        "/v1/people": _get_people,
        "/health": _get_health,
        "/v1/speak_queue": _get_speak_queue,
        "/v1/location": _get_location,
    }
    _PUT_ROUTES = {"/v1/settings": _put_settings}
    _POST_ROUTES = {
        "/v1/chat": _post_chat,
        "/v1/people": _post_people,
        "/v1/vision": _post_vision,
        "/v1/speak": _post_speak,
        "/v1/transcribe": _post_transcribe,
        "/v1/location": _post_location,
        "/v1/announce": _post_announce,
    }

    def _dispatch(self, routes: dict[str, Any], match: str) -> None:
        route = routes.get(match)
        if route is None:
            self._send(404, {"error": "not_found"})
            return
        route(self, urllib.parse.urlsplit(self.path))

    def do_OPTIONS(self) -> None:  # noqa: N802
        # CORS preflight for the G2 app's cross-origin calls. Answered for
        # any path: the preflight carries no credentials and grants nothing
        # by itself -- the actual request still hits the bearer check.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-Device-Id, X-Sample-Rate")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # GET is the only method that arrives with a query string, and
        # matching POST and PUT on the raw path is what they already did.
        parsed = urllib.parse.urlsplit(self.path)
        # /g2/* is the one prefix route: a static bundle with hashed asset
        # names cannot be enumerated in an exact-match table.
        if parsed.path == "/g2" or parsed.path.startswith("/g2/"):
            self._get_g2(parsed)
            return
        self._dispatch(self._GET_ROUTES, parsed.path)

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch(self._PUT_ROUTES, self.path)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self._POST_ROUTES, self.path)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never print Authorization headers, provider keys, or full prompts.
        _log(f"gateway {self.command} {self.path} {args[1] if len(args) > 1 else ''}")


VOICEVOX_DICT_PATH = os.environ.get(
    "VOICEVOX_DICT_FILE", os.path.join(os.path.dirname(__file__), "voicevox_dict.json"))


def _register_voicevox_words(env: dict[str, str]) -> None:
    """Teach VOICEVOX how to read words it gets wrong.

    Names are the common case: VOICEVOX reads 大輔 as "オオスケ", so the device
    called its owner by the wrong name every time it used it. The engine keeps
    its user dictionary in its own state, but that is invisible to this repo
    and lost on a reinstall, so the intended readings live in a file here and
    are re-applied at startup.

    Format is a plain {"surface": "pronunciation"} map, pronunciation in
    katakana:  {"大輔": "ダイスケ"}
    """
    try:
        # utf-8-sig, not utf-8: this file gets hand-edited on Windows, and
        # PowerShell's Set-Content -Encoding utf8 writes a BOM that plain
        # utf-8 decoding turns into a JSONDecodeError on the first character.
        with open(VOICEVOX_DICT_PATH, encoding="utf-8-sig") as f:
            words = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        _log(f"gateway voicevox dict unreadable ({type(exc).__name__}); skipping")
        return
    if not isinstance(words, dict):
        return
    base_url = env.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
    added = 0
    for surface, pronunciation in words.items():
        if not isinstance(surface, str) or not isinstance(pronunciation, str):
            continue
        params = urllib.parse.urlencode({"surface": surface, "pronunciation": pronunciation,
                                         "accent_type": 0})
        try:
            request = urllib.request.Request(f"{base_url}/user_dict_word?{params}", data=b"", method="POST")
            with urllib.request.urlopen(request, timeout=10):
                added += 1
        except Exception:
            # Duplicate entries and a stopped engine both land here; neither
            # is worth failing startup over.
            continue
    if added:
        _log(f"gateway voicevox dictionary entries applied={added}")


def _warm_voicevox(env: dict[str, str]) -> None:
    """Preload the configured VOICEVOX voice at startup.

    The engine loads a speaker's model on first use, so without this the
    first sentence of the first reply of the day pays that cost while the
    user waits.
    """
    if env.get("TTS_PROVIDER", "").lower() != "voicevox" and not env.get("VOICEVOX_URL"):
        return
    base_url = env.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
    speaker = env.get("VOICEVOX_SPEAKER", "3")
    url = f"{base_url}/initialize_speaker?{urllib.parse.urlencode({'speaker': speaker})}"
    try:
        request = urllib.request.Request(url, data=b"", method="POST")
        t0 = time.monotonic()
        with urllib.request.urlopen(request, timeout=60):
            pass
        _log(f"gateway voicevox speaker={speaker} preloaded in {(time.monotonic() - t0) * 1000:.0f}ms")
        _register_voicevox_words(env)
    except Exception as exc:
        _log(f"gateway voicevox preload failed ({type(exc).__name__}); "
             f"TTS will fall back to gemini until the engine is reachable")


def _warm_biometrics() -> None:
    """Load the biometric models at boot instead of inside the first question.

    They load on first use, and first use is inside /v1/transcribe -- the
    request somebody is already waiting on, having just spoken. Measured on
    this machine: 12.8s for the first voice embedding against 0-16ms for every
    one after it. So the first conversation after a restart looked like the
    robot had hung, while every later one was fine, which is a maddening thing
    to debug from the outside. Worse, a wait that long can outlast the device's
    own HTTP timeout, and then there is no reply at all rather than a late one.

    On a thread, because the device polls every two seconds and should find the
    server already listening rather than blocked on torch. The loaders take a
    lock, so a real request that arrives mid-warm waits for this same load
    instead of starting a second one.
    """
    def load() -> None:
        t0 = time.monotonic()
        voice = biometrics.voice_available()
        if voice:
            # Loading the encoder is only half of it. voice_embedding() also
            # goes through librosa, which leans on numba, which compiles on
            # first use -- so warming the model left the path through it stone
            # cold and the first person to speak after a restart paid for the
            # compile inside the request they were waiting on. Measured here:
            # voice_available() 7.8s, then the first real embedding another
            # 14.9s, and 16ms for every one after it. On the device that showed
            # up as 67 seconds between the transcription finishing and the
            # reply being sent, long enough that it gave up and said nothing.
            #
            # So put a real clip through it. Silence is enough to make every
            # layer run; what matters is that nothing is left to compile.
            biometrics.voice_embedding(b"\x00\x00" * 16000 * 2, 16000)
        voice_ms = (time.monotonic() - t0) * 1000
        t1 = time.monotonic()
        face = biometrics.face_available()
        face_ms = (time.monotonic() - t1) * 1000
        _log(f"gateway biometrics warmed voice={voice} in {voice_ms:.0f}ms "
             f"face={face} in {face_ms:.0f}ms")
        # Say what a False actually costs, at the volume it deserves. Without
        # the voice encoder nobody is ever identified, so every listener is
        # treated as a stranger and master-only facts -- a name, an address, a
        # birthday -- are withheld from the model exactly as designed. From the
        # outside that is indistinguishable from amnesia, and it went unnoticed
        # for a day because the only trace was "voice=False" in one line.
        if not voice and _speaker_id_enabled(dict(os.environ)):
            _log("gateway WARNING speaker identification is OFF: the voice encoder "
                 "could not be loaded, so nobody will be recognised and master-only "
                 "memories stay hidden. Usually a python without torch/librosa -- "
                 "check the 'python:' line from run_gateway.ps1.")

    threading.Thread(target=load, daemon=True).start()


def _announce_memory_store() -> None:
    """Settle and report which store this gateway is using, at startup.

    Adoption of a pre-sharing store used to happen the first time memory was
    read, which is the first time somebody speaks -- so after moving a store
    between machines there was no way to confirm it had been carried over
    except to ask the robot something and hope. Do it at boot instead, where
    the answer can be read before anyone is relying on it.
    """
    if not _memory_enabled(dict(os.environ)):
        _log("gateway memory disabled; nothing is remembered between requests")
        return
    path = _memory_path("")
    with _memory_lock:
        _adopt_legacy_device_store(path)
    if os.path.exists(path):
        _log(f"gateway memory store: {os.path.basename(path)}")
    else:
        _log(f"gateway memory store: {os.path.basename(path)} (new, nothing remembered yet)")


def main() -> None:
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    _log(f"Tachikoma Gateway listening on {host}:{port} (provider={os.environ.get('AI_PROVIDER', 'mock')})")
    _announce_memory_store()
    _warm_biometrics()
    _warm_voicevox(dict(os.environ))
    if _debug_logging_enabled(os.environ):
        _log(f"TACHIKOMA_DEBUG_LOGGING=1: recognized speech text will be logged and uploaded audio "
             f"saved to {DEBUG_AUDIO_DIR} -- investigation-only, disable when done")
    ThreadingHTTPServer((host, port), GatewayHandler).serve_forever()


if __name__ == "__main__":
    main()
