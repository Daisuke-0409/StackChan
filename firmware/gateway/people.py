"""Who is talking, and what may be said in front of them.

Two things live here and they are deliberately separate:

  identity  -- which known person this voice/face belongs to, or nobody.
  policy    -- what a person of that standing is allowed to be told.

Identity is a guess. It is made from biometrics off a 0.3MP camera and a
few seconds of speech, either of which can be fooled by a photo or a
recording, so it is treated as a convenience signal and never as
authentication. Policy is not a guess: a fact tagged for the master is
simply not loaded when anyone else is speaking, so the model answering the
question has never seen it. Nothing is being asked to keep a secret; the
secret is not in the room.

Everything here is personal data about people who are not the operator --
a partner, colleagues, visitors. It stays on this machine, out of git, and
out of the logs, which record decisions and never the data behind them.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

# Standing, most privileged first. A fact carries the *widest* audience it
# may reach, and a listener sees a fact when their standing is at least as
# trusted as that audience.
ROLE_MASTER = "master"
ROLE_HOUSEHOLD = "household"
ROLE_COLLEAGUE = "colleague"
ROLE_GUEST = "guest"
ROLE_UNKNOWN = "unknown"

_ROLE_RANK = {
    ROLE_MASTER: 4,
    ROLE_HOUSEHOLD: 3,
    ROLE_COLLEAGUE: 2,
    ROLE_GUEST: 1,
    ROLE_UNKNOWN: 0,
}

# Audience a fact may reach. "master" is the tightest.
VISIBILITY_MASTER = "master"
VISIBILITY_HOUSEHOLD = "household"
VISIBILITY_COLLEAGUE = "colleague"
VISIBILITY_EVERYONE = "everyone"

_VISIBILITY_MIN_RANK = {
    VISIBILITY_MASTER: _ROLE_RANK[ROLE_MASTER],
    VISIBILITY_HOUSEHOLD: _ROLE_RANK[ROLE_HOUSEHOLD],
    VISIBILITY_COLLEAGUE: _ROLE_RANK[ROLE_COLLEAGUE],
    # 0, not the guest rank: "everyone" has to include the person nobody
    # recognized, or the assistant goes mute in front of a stranger instead
    # of merely discreet. Anything that must not reach a stranger belongs at
    # colleague or above -- that is what those levels are for.
    VISIBILITY_EVERYONE: _ROLE_RANK[ROLE_UNKNOWN],
}

DEFAULT_VISIBILITY = VISIBILITY_MASTER

# Cosine thresholds, measured against the encoder in biometrics.py using
# distinct synthetic voices: same speaker scored 0.64-0.83, clearly different
# speakers 0.38-0.46, while two voices of the same type and register reached
# 0.77 against each other. So a single threshold cannot separate everybody --
# which is why identify() also demands a margin over the runner-up and
# reports nobody when two enrolled people sound alike. Refusing is correct
# there; guessing is how the wrong person hears something private.
VOICE_MATCH_THRESHOLD = 0.62
VOICE_MATCH_MARGIN = 0.06
# SFace's own documented cosine threshold for "same person".
FACE_MATCH_THRESHOLD = 0.363
FACE_MATCH_MARGIN = 0.05

# Said out loud, these mark what follows as private. Keyword matching rather
# than asking the model to judge: a missed cue would publish something that
# was meant to stay in, and a false positive only over-restricts. The model's
# own judgement can be layered on later as an *additional* trigger, never as
# a replacement.
# Both spellings of every word: speech recognition picks kanji or kana by
# context, and the operator does not get to know which it chose. A cue that
# only matches 話 silently fails on はなし, which is the same phrase.
_SECRET_PATTERNS = [
    r"ここだけの(?:話|はなし)", r"内緒", r"ないしょ", r"秘密", r"ひみつ",
    r"(?:二人|ふたり|2人)(?:だけ|の秘密|のひみつ)",
    r"(?:俺|おれ|私|わたし|僕|ぼく)と(?:お前|おまえ|君|きみ|あなた)だけ",
    r"他(?:の人|人)には(?:言わない|言うな|いわない|内緒|ないしょ|秘密|ひみつ)",
    r"誰にも(?:言わない|言うな|いわない|いうな)",
    r"だれにも(?:言わない|いわない)",
    r"オフレコ", r"言わないで", r"いわないで",
]
_SECRET_RE = re.compile("|".join(_SECRET_PATTERNS))

# Cancels the above, for when the operator wants a fact shared again.
# The "彼女にも" branch already carries its own に, so requiring another one
# after the group meant it could never match -- "彼女にも話していいよ" fell
# through and the fact stayed closed.
_UNSECRET_RE = re.compile(
    r"(?:もう)?(?:みんな|全員|誰にでも|皆|彼女|家族|みなさん)に(?:も)?\s*(?:言って|話して|教えて|共有して)(?:いい|ok|OK|大丈夫)")


# Words that merely contain a cue without asking for one. Checked first, so
# "秘密基地の話をして" is a request about a treehouse rather than an
# instruction to keep it. Known and accepted: a genuine question *about* the
# word ("内緒話って英語でなんて言うの") still trips the cue. Over-restricting
# only means a fact stays more private than it needed to, which is the
# direction this is supposed to fail in.
_SECRET_EXCEPTIONS = re.compile(r"秘密基地|ひみつ基地|秘密兵器")


def mentions_secret(text: str) -> bool:
    text = text or ""
    if _SECRET_EXCEPTIONS.search(text) and not re.search(r"ここだけ|オフレコ|誰にも|だれにも", text):
        return False
    return bool(_SECRET_RE.search(text))


def mentions_unsecret(text: str) -> bool:
    return bool(_UNSECRET_RE.search(text or ""))


def can_hear(role: str, visibility: str) -> bool:
    """May a listener of this standing be told a fact with this audience?"""
    listener = _ROLE_RANK.get(role, 0)
    required = _VISIBILITY_MIN_RANK.get(visibility, _ROLE_RANK[ROLE_MASTER])
    return listener >= required


def default_visibility_for(role: str) -> str:
    """How private a fact learned from this speaker should be by default.

    Something the master says is his to keep, so it starts closed. Something
    a colleague volunteers about themselves is not a secret of the
    household's, and locking it to the master would just make the assistant
    unable to use it when that same colleague comes back.
    """
    if role == ROLE_MASTER:
        return VISIBILITY_MASTER
    if role == ROLE_HOUSEHOLD:
        return VISIBILITY_HOUSEHOLD
    if role == ROLE_COLLEAGUE:
        return VISIBILITY_COLLEAGUE
    return VISIBILITY_EVERYONE


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return dot / (na * nb)


class PeopleStore:
    """The people this device has met, and how to recognize them again.

    One JSON file, loaded and rewritten under a lock. A person accumulates
    several embeddings per modality rather than one average: a voice sounds
    different across a cold, a room, a distance, and keeping the samples
    separate matches better than blurring them together.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    # --- storage ---------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            with open(self._path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"people": {}}
        if not isinstance(data, dict) or not isinstance(data.get("people"), dict):
            return {"people": {}}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)

    # --- people ----------------------------------------------------------
    def list_people(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load()["people"].values())

    def get(self, person_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._load()["people"].get(person_id)

    def add_person(self, name: str, role: str = ROLE_GUEST) -> str:
        person_id = uuid.uuid4().hex[:12]
        with self._lock:
            data = self._load()
            data["people"][person_id] = {
                "id": person_id,
                "name": name,
                "role": role if role in _ROLE_RANK else ROLE_GUEST,
                "voice": [],
                "face": [],
                "first_seen": time.time(),
                "last_seen": time.time(),
                "encounters": 0,
            }
            self._save(data)
        return person_id

    def set_role(self, person_id: str, role: str) -> bool:
        if role not in _ROLE_RANK:
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            person["role"] = role
            self._save(data)
        return True

    def add_embedding(self, person_id: str, modality: str, embedding: list[float],
                      *, max_samples: int = 8) -> bool:
        """Remember another sample of this person's voice or face."""
        if modality not in ("voice", "face") or not embedding:
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            samples = person.setdefault(modality, [])
            samples.append([float(v) for v in embedding])
            # Keep the most recent: a voice drifts, and old samples from a
            # different room stop helping once there are better ones.
            del samples[:-max_samples]
            person["last_seen"] = time.time()
            self._save(data)
        return True

    def delete_person(self, person_id: str) -> bool:
        """Forget someone entirely, biometrics included.

        The only way to withdraw a face and a voice that were recorded
        without their owner sitting at this keyboard, so it deletes rather
        than deactivates.
        """
        with self._lock:
            data = self._load()
            if person_id not in data["people"]:
                return False
            del data["people"][person_id]
            self._save(data)
        return True

    def note_encounter(self, person_id: str) -> None:
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return
            person["encounters"] = int(person.get("encounters", 0)) + 1
            person["last_seen"] = time.time()
            self._save(data)

    # --- identification --------------------------------------------------
    def identify(self, modality: str, embedding: list[float], threshold: float,
                 margin: float = 0.05) -> tuple[Optional[dict[str, Any]], float]:
        """Best match for an embedding, or (None, score) when unsure.

        Two conditions, not one. The best match must clear `threshold`, and
        it must also beat the runner-up by `margin` -- two people scoring
        0.71 and 0.70 is not an identification, it is a coin toss, and
        resolving it by picking the larger number is how the wrong person
        gets told something private. Undecided means unknown, which means
        least privilege.
        """
        if not embedding:
            return None, 0.0
        scored: list[tuple[float, dict[str, Any]]] = []
        for person in self.list_people():
            samples = person.get(modality) or []
            if not samples:
                continue
            best = max(cosine_similarity(embedding, s) for s in samples)
            scored.append((best, person))
        if not scored:
            return None, 0.0
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_person = scored[0]
        if best_score < threshold:
            return None, best_score
        if len(scored) > 1 and best_score - scored[1][0] < margin:
            return None, best_score
        return best_person, best_score
