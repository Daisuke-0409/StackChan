"""What the user just did to the order (再設計 §11-§15).

One utterance in, one operation out: "あ、やっぱダブチ" becomes
REPLACE(item_1, ダブチ), and `draft.py` decides what that does. Nothing
here edits an order; nothing here talks to a menu, a store or a model.

Almost all of this is lexical, so almost all of it is plain Python. The
words that mark a correction (やっぱ, いや, じゃなくて) and the words that
mark a removal (いらない, やめて) are a closed set that a person can read
and a test can pin down. Sending them to an LLM would make the same
decision more slowly, less repeatably, and with a bill attached (§37-7).

The model earns its place further along, where the question is genuinely
open: which product on this store's real menu did "チキンのやつ" mean
(§18, `ai_match.py`). That needs the menu, which arrives in STEP 7.

The other rule is that being unsure is an outcome, not a coin flip. When
an utterance could be a new item or a change to the current one, this says
so and lets the caller ask. Guessing wrong here does not produce a
confusing sentence; it produces a burger nobody ordered.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from . import draft as draft_mod

# The operations from §11 that this module can produce. QUERY (STEP 5),
# CONDITIONAL (STEP 6) and CONFIRM (already handled by the approval gate)
# are deliberately absent -- an interpreter that returned them now would
# have nothing downstream to receive them.
ADD = "ADD"
REPLACE = "REPLACE"
MODIFY = "MODIFY"
REMOVE = "REMOVE"

# §12. Said before naming what was actually wanted, so they turn the next
# product into a correction of the current line instead of a new one.
_CORRECTION_RE = re.compile(
    r"やっぱ(り)?|いや[、。\s]|あ[、。\s]*違う|じゃなくて|ではなく|訂正|"
    r"やめて|やめた|こっちにして|に変えて|の方で")

# Removal is stated, never inferred. Note やめて appears above too: it marks
# a correction when a replacement follows and a removal when one does not,
# which is why removal is tested first.
_REMOVE_RE = re.compile(r"いらない|要らない|なしで|抜いて|やめとく|キャンセル(して)?|削除|"
                        r"(は|を)?\s*やめて|外して")

# §11 ADD. "あと" and "も" are the ordinary Japanese for one more thing.
_ADD_RE = re.compile(r"あと|それと|追加|も(お願い|ちょうだい|つけて)|"
                     r"ついでに|プラス")

# §14. Words that point at the line under discussion rather than name it.
_REFERENCE_RE = re.compile(r"それ|そっち|こっち|さっきの|その|この")

# Sizes are a closed set and the one modification that needs no menu.
_SIZE_RE = re.compile(r"(?<![A-Za-z])([SMLsml])(?![A-Za-z])|エス|エム|エル|"
                      r"(小|中|大)(?:サイズ|きい|きく)?")
_SIZE_WORDS = {"エス": "S", "エム": "M", "エル": "L", "小": "S", "中": "M", "大": "L"}

_QUANTITY_RE = re.compile(r"([0-9０-９]+)\s*(?:個|つ|杯|点|セット)")

# "Lにして", "2個にして" -- the verb that marks a change to what is already
# there rather than a request for something new.
_MODIFY_VERB_RE = re.compile(r"にして|に変えて|でお願い|にしといて")


@dataclass
class Utterance:
    """A decision about one turn, with the doubt kept rather than hidden."""

    action: Optional[str] = None
    target_item_id: Optional[str] = None       # None = the active item
    product: Optional[str] = None              # ADD / REPLACE
    changes: dict[str, Any] = field(default_factory=dict)  # MODIFY
    ambiguous: bool = False
    question: Optional[str] = None             # what to ask, if ambiguous
    needs_menu: bool = False                   # resolvable only with a menu

    def is_actionable(self) -> bool:
        return self.action is not None and not self.ambiguous


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _extract_size(text: str) -> Optional[str]:
    match = _SIZE_RE.search(text)
    if not match:
        return None
    letter, word = match.group(1), match.group(0)
    if letter:
        return letter.upper()
    for spoken, size in _SIZE_WORDS.items():
        if spoken in word:
            return size
    return None


def _extract_quantity(text: str) -> Optional[int]:
    match = _QUANTITY_RE.search(text)
    if not match:
        return None
    value = int(unicodedata.normalize("NFKC", match.group(1)))
    return value if 1 <= value <= draft_mod.MAX_QUANTITY else None


# People name a line by its opening, not its full name: "ビッグマックの方"
# for ビッグマックセット. Short prefixes match too much (ポ, ポテ), so the
# shortest that can identify a product is the floor.
_MIN_NAME_PREFIX = 3


def _find_named_item(text: str, order: draft_mod.OrderDraft) -> Optional[str]:
    """The line the user named outright ("ビッグマックの方Lにして").

    Scored by how much of the product name is actually present, longest
    first, so "ビッグマックセットをLに" picks the セット over the plain
    burger when the draft holds both.
    """
    best_id, best_score = None, 0
    for item in order.items:
        name = item.product or ""
        for length in range(len(name), _MIN_NAME_PREFIX - 1, -1):
            if name[:length] in text:
                if length > best_score:
                    best_id, best_score = item.id, length
                break
    return best_id


# "ビッグマックの方" names a line; the の方 is not part of the product.
_NAMING_SUFFIX_RE = re.compile(r"の方|のほう")
# "あ、やっぱ…" -- the あ is a breath, not an order.
# Must be followed by a break, or "あとナゲット" loses its あ and the
# addition marker with it.
_INTERJECTION_RE = re.compile(r"^(?:ああ|あー|あ|えーと|えっと|うーん|んー)(?=[、。\s])[、。\s]*")


def _residual_product(text: str) -> str:
    """What is left after stripping the words that carried the intent.

    Sizes go too. Left in, "Lにして" reads as an order for a product called
    L, and the modification branch never fires.
    """
    residue = _INTERJECTION_RE.sub(" ", text)
    for pattern in (_CORRECTION_RE, _ADD_RE, _REFERENCE_RE, _MODIFY_VERB_RE,
                    _QUANTITY_RE, _SIZE_RE, _NAMING_SUFFIX_RE):
        residue = pattern.sub(" ", residue)
    residue = re.sub(r"[、。！？!?]", " ", residue)
    residue = re.sub(r"\s+", " ", residue).strip()
    return residue.strip("をがはでにねよ ")


def classify(text: str, order: draft_mod.OrderDraft) -> Utterance:
    """Turn one utterance into one operation against `order`.

    The order of the checks is the whole design. Removal is stated
    outright, so it is read first and never inferred from anything softer.
    A correction marker then turns whatever product follows into a
    replacement of the line being discussed -- that single rule is what
    stops "ビッグマック / あ、やっぱビッグマックセット" becoming two burgers.
    """
    normalized = _normalize(text)
    named_target = _find_named_item(normalized, order)
    refers_to_active = bool(_REFERENCE_RE.search(normalized))
    target = named_target  # None means "the active item"

    # --- REMOVE ---------------------------------------------------------
    if _REMOVE_RE.search(normalized):
        if not order.items:
            return Utterance(ambiguous=True, question="まだ何も注文に入っていないよ。")
        if target is None and order.active_item is None:
            return Utterance(ambiguous=True, question="どれを取り消す？")
        return Utterance(action=REMOVE, target_item_id=target)

    correcting = bool(_CORRECTION_RE.search(normalized))
    adding = bool(_ADD_RE.search(normalized))
    size = _extract_size(normalized)
    quantity = _extract_quantity(normalized)
    product = _residual_product(normalized)

    if product and named_target:
        named_product = order.require(named_target).product
        if product in named_product:
            product = ""

    # --- MODIFY ---------------------------------------------------------
    # A size or a count with no product left over is a change to the line
    # in hand, not a new one. "L にして" cannot be ordered by itself.
    if not product and (size or quantity):
        if target is None and order.active_item is None:
            return Utterance(ambiguous=True, question="どれのこと？")
        changes: dict[str, Any] = {}
        if size:
            changes["size"] = size
        if quantity:
            changes["quantity"] = quantity
        return Utterance(action=MODIFY, target_item_id=target, changes=changes)

    if not product:
        # Nothing nameable and nothing measurable: not an order edit.
        return Utterance()

    # --- REPLACE --------------------------------------------------------
    if correcting and order.items:
        if target is None and order.active_item is None:
            return Utterance(ambiguous=True, question="どれを変える？")
        changes = {"size": size} if size else {}
        return Utterance(action=REPLACE, target_item_id=target, product=product,
                         changes=changes)

    # --- ADD ------------------------------------------------------------
    if adding or not order.items:
        return Utterance(action=ADD, product=product,
                         changes={"quantity": quantity} if quantity else {})

    # --- unmarked, with something already in the draft -------------------
    # No correction word, no "あと". "ビッグマックセット" straight after
    # "ビッグマック" could be either, and the two readings differ by one
    # burger. Where the words overlap, say so and ask (§15).
    active = order.get(target) if target else order.active_item
    if active and _overlaps(product, active.product):
        return Utterance(
            ambiguous=True, product=product, target_item_id=target,
            question=f"{active.product}を{product}に変える？それとも追加する？")

    # A bare word while a set is being discussed is usually one of its
    # choices ("コーラゼロ"), but only the store's menu knows whether that
    # is a drink, a side or a product of its own (§18).
    if active and active.variant == "set":
        return Utterance(product=product, target_item_id=target, needs_menu=True)

    if refers_to_active:
        return Utterance(ambiguous=True, product=product,
                         question=f"{product}は、どれのこと？")

    return Utterance(action=ADD, product=product,
                     changes={"quantity": quantity} if quantity else {})


def _overlaps(product: str, other: str) -> bool:
    """One name contains the other -- a refinement rather than a new thing."""
    if not product or not other:
        return False
    return product in other or other in product


def apply(utterance: Utterance, order: draft_mod.OrderDraft) -> draft_mod.DraftItem:
    """Carry out an actionable utterance. Raises DraftError if it is not.

    Kept separate from classify() so that the decision can be inspected,
    logged and tested without anything changing.
    """
    if not utterance.is_actionable():
        raise draft_mod.DraftError("実行できる操作ではありません")
    if utterance.action == ADD:
        return order.add(utterance.product,
                         quantity=utterance.changes.get("quantity", 1))
    if utterance.action == REPLACE:
        return order.replace(utterance.product, utterance.target_item_id,
                             **utterance.changes)
    if utterance.action == MODIFY:
        return order.modify(utterance.target_item_id, **utterance.changes)
    if utterance.action == REMOVE:
        return order.remove(utterance.target_item_id)
    raise draft_mod.DraftError(f"未知の操作: {utterance.action}")
