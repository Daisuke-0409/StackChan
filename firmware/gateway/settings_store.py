"""What the operator can change about Tachikoma, and where it is kept.

The point of this file is the schema, not the storage. Every setting is
declared once here -- type, label, default, help text, which group it
belongs to -- and the web UI is generated from that declaration rather than
written by hand. Adding a setting later means adding one entry below; no UI
work, no new endpoint, no app release. That is the whole reason it is built
this way, because the list of things worth adjusting on this robot is going
to keep growing.

Values live in one JSON file next to the memory store. Anything unset falls
back to the declared default, so a fresh install behaves exactly like the
hard-coded behaviour it replaces.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Optional

SETTINGS_PATH = os.environ.get(
    "TACHIKOMA_SETTINGS_FILE",
    os.path.join(os.path.dirname(__file__), "memory", "settings.json"))

# Groups are rendered as sections, in this order.
GROUPS = [
    ("voice", "声"),
    ("behaviour", "ふるまい"),
    ("persona", "人格"),
    ("privacy", "記憶とプライバシー"),
]

# type: bool | text | textarea | select | number
# options: for select, either a literal list of {value,label} or the name of
#          a dynamic provider registered in _OPTION_PROVIDERS.
SETTING_DEFS: list[dict[str, Any]] = [
    {
        "key": "speech_enabled", "group": "voice", "type": "bool", "default": True,
        "label": "声を出す",
        "help": "オフにすると、返事は作るけれど喋りません。表示と記憶はそのまま動きます。",
    },
    {
        "key": "voice_speaker", "group": "voice", "type": "select", "default": "3",
        "label": "声の種類", "options": "voicevox_speakers",
        "help": "VOICEVOX の話者。エンジンが動いていないときは選べません。",
    },
    {
        "key": "voice_speed", "group": "voice", "type": "number", "default": 1.0,
        "min": 0.5, "max": 2.0, "step": 0.05,
        "label": "話す速さ", "help": "1.0 が標準です。",
    },
    {
        "key": "motion_enabled", "group": "behaviour", "type": "bool", "default": True,
        "label": "体を動かす",
        "help": "マナーモードと同じです。オフでも会話と表情の判定は続きます。",
    },
    {
        "key": "emotion_enabled", "group": "behaviour", "type": "bool", "default": True,
        "label": "感情で動く",
        "help": "返事の内容から嬉しい・困ったを判定して、しぐさに出します。",
    },
    {
        "key": "face_tracking_enabled", "group": "behaviour", "type": "bool", "default": True,
        "label": "顔を追う", "help": "話している相手の方を向きます。",
    },
    {
        "key": "web_search_enabled", "group": "behaviour", "type": "bool", "default": True,
        "label": "調べものをする",
        "help": "最新の情報が必要な質問のときだけ検索します。普段の会話は遅くなりません。",
    },
    {
        "key": "first_person", "group": "persona", "type": "text", "default": "",
        "label": "一人称", "placeholder": "僕 / 私 / オレ / ボク など",
        "help": "空欄なら指定しません。",
    },
    {
        "key": "persona", "group": "persona", "type": "textarea", "default": "",
        "label": "性格・話し方",
        "placeholder": "例: 好奇心が強くて、少し子どもっぽい。相手を「〜だね」と親しく呼ぶ。",
        "help": "そのまま人格の指示になります。長すぎると返事が遅くなります。",
    },
    {
        "key": "memory_enabled", "group": "privacy", "type": "bool", "default": True,
        "label": "会話を覚える",
        "help": "オフにすると、この先の会話は記憶されません。覚えた内容は消えません。",
    },
    {
        "key": "speaker_id_enabled", "group": "privacy", "type": "bool", "default": True,
        "label": "声で相手を見分ける",
        "help": "オフにすると全員が同じ扱いになり、相手ごとの出し分けが止まります。",
    },
]

_OPTION_PROVIDERS: dict[str, Callable[[], list[dict[str, str]]]] = {}
_lock = threading.Lock()


def register_option_provider(name: str, provider: Callable[[], list[dict[str, str]]]) -> None:
    """Supplies choices that are only known at runtime, e.g. installed voices."""
    _OPTION_PROVIDERS[name] = provider


def _defaults() -> dict[str, Any]:
    return {d["key"]: d["default"] for d in SETTING_DEFS}


def load() -> dict[str, Any]:
    values = _defaults()
    try:
        with _lock, open(SETTINGS_PATH, encoding="utf-8-sig") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return values
    if isinstance(stored, dict):
        for definition in SETTING_DEFS:
            key = definition["key"]
            if key in stored:
                values[key] = stored[key]
    return values


def get(key: str) -> Any:
    return load().get(key)


def _coerce(definition: dict[str, Any], value: Any) -> Any:
    kind = definition["type"]
    if kind == "bool":
        return bool(value)
    if kind == "number":
        number = float(value)
        if "min" in definition:
            number = max(number, float(definition["min"]))
        if "max" in definition:
            number = min(number, float(definition["max"]))
        return number
    # text/textarea/select all arrive as strings; cap length so a paste
    # accident cannot push a novel into every system prompt.
    return str(value)[:2000]


def update(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Applies a partial update. Returns (new values, rejected keys)."""
    by_key = {d["key"]: d for d in SETTING_DEFS}
    rejected = [k for k in patch if k not in by_key]
    values = load()
    for key, raw in patch.items():
        if key not in by_key:
            continue
        try:
            values[key] = _coerce(by_key[key], raw)
        except (TypeError, ValueError):
            rejected.append(key)
    with _lock:
        directory = os.path.dirname(SETTINGS_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{SETTINGS_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(values, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
    return values, rejected


def schema() -> dict[str, Any]:
    """The UI is built from this, so a new setting needs no UI change."""
    groups = []
    for group_key, group_label in GROUPS:
        items = []
        for definition in SETTING_DEFS:
            if definition["group"] != group_key:
                continue
            item = {k: v for k, v in definition.items() if k != "options"}
            options = definition.get("options")
            if isinstance(options, str):
                provider = _OPTION_PROVIDERS.get(options)
                item["options"] = provider() if provider else []
            elif options is not None:
                item["options"] = options
            items.append(item)
        if items:
            groups.append({"key": group_key, "label": group_label, "settings": items})
    return {"groups": groups}
