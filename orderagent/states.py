"""Which states an order can be in, and which it can move to (再設計 §23, §26).

The states keep the names the code and the audit log have used since the
first order: created, building, awaiting_approval, and the rest. The
redesign proposes IDLE/DRAFTING/QUOTE_READY/AWAITING_FINAL_APPROVAL, which
mean the same things -- awaiting_approval already is the state where, and
only where, an approval counts. Renaming would split the audit trail into
before and after without making a single order safer, so the only addition
is `drafting`, for the part of the conversation that did not exist before.

Two properties are worth stating because they are the ones a person would
want proved rather than promised:

  1. Approval is only heard in awaiting_approval. Saying "OK" in ordinary
     conversation, or "注文して" while a cart is still being built, cannot
     buy anything -- there is no state in which those words reach money.

  2. There is exactly one edge into verifying, and it comes from
     awaiting_approval. So payment is attempted at most once per approval,
     and an uncertain outcome has no path back. §27 forbids automatic
     retries; here that is not a rule the code follows but a road that
     does not exist.

A payment outcome is three-valued (§26). CONFIRMED and FAILED are the easy
ones. UNKNOWN is the one that matters: the request went out and no answer
came back, so the store may or may not be making the food. Treating that
as failure and retrying is how a person ends up paying twice.
"""
from __future__ import annotations

from typing import Optional

# --- states ----------------------------------------------------------------

CREATED = "created"
DRAFTING = "drafting"                      # new: the conversation (§10)
NEEDS_INFO = "needs_info"
BUILDING = "building"
AWAITING_APPROVAL = "awaiting_approval"    # = the spec's AWAITING_FINAL_APPROVAL
VERIFYING = "verifying"
DRY_RUN_DONE = "dry_run_done"
PAID = "paid"
PAYMENT_UNCERTAIN = "payment_uncertain"    # = the spec's UNKNOWN (§26)
FAILED = "failed"
ESCALATED = "escalated"
DENIED = "denied"
EXPIRED = "expired"

# --- payment outcomes (§26) ------------------------------------------------

CONFIRMED = "CONFIRMED"
OUTCOME_FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

_OUTCOME_STATES = {
    CONFIRMED: PAID,
    OUTCOME_FAILED: FAILED,
    UNKNOWN: PAYMENT_UNCERTAIN,
}

# --- the machine -----------------------------------------------------------

TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({DRAFTING, BUILDING, NEEDS_INFO, FAILED, DENIED}),
    # Each utterance leaves the draft in the same state; the self-edge is
    # what makes a many-turn conversation legal rather than exceptional.
    DRAFTING: frozenset({DRAFTING, BUILDING, NEEDS_INFO, FAILED, DENIED, EXPIRED}),
    NEEDS_INFO: frozenset({DRAFTING, BUILDING, FAILED, DENIED, EXPIRED}),
    BUILDING: frozenset({AWAITING_APPROVAL, NEEDS_INFO, FAILED, ESCALATED, DENIED}),
    # The only edge into VERIFYING in the whole table.
    AWAITING_APPROVAL: frozenset({VERIFYING, DENIED, EXPIRED, FAILED}),
    VERIFYING: frozenset({DRY_RUN_DONE, PAID, PAYMENT_UNCERTAIN, FAILED, ESCALATED}),
    # Only reconciliation may move this, and only by finding out what
    # already happened -- never by trying again (§27, STEP 11).
    PAYMENT_UNCERTAIN: frozenset({PAID, FAILED, ESCALATED}),
    DRY_RUN_DONE: frozenset(),
    PAID: frozenset(),
    FAILED: frozenset(),
    ESCALATED: frozenset(),
    DENIED: frozenset(),
    EXPIRED: frozenset(),
}

#: States an order can sit in forever. Nothing further will happen on its own.
TERMINAL = frozenset(state for state, nxt in TRANSITIONS.items() if not nxt)

#: States in which a job still occupies the agent.
ACTIVE = frozenset({CREATED, DRAFTING, NEEDS_INFO, BUILDING,
                    AWAITING_APPROVAL, VERIFYING})


class IllegalTransition(RuntimeError):
    """A move the machine does not have. The job did not change."""


def can(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def advance(current: str, target: str) -> str:
    """The new state, or refuse. Never silently stays put.

    Refusing loudly matters more here than elsewhere: a transition that
    quietly does nothing leaves a job looking approved when it is not, and
    everything downstream of that reads the wrong state as consent.
    """
    if not can(current, target):
        raise IllegalTransition(f"{current} から {target} へは遷移できません")
    return target


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def is_active(state: str) -> bool:
    return state in ACTIVE


def accepts_approval(state: str) -> bool:
    """Whether an approval phrase means anything right now (§23).

    One state, deliberately. "OK" during ordinary conversation, and even
    "注文して" while the cart is still being built, are heard as words and
    nothing else.
    """
    return state == AWAITING_APPROVAL


def state_for_outcome(outcome: str) -> str:
    """The state a payment attempt leaves the job in (§26)."""
    if outcome not in _OUTCOME_STATES:
        raise ValueError(f"未知の決済結果: {outcome}")
    return _OUTCOME_STATES[outcome]


def may_retry_payment(state: str) -> bool:
    """Always False. Kept as a function so the answer has one place to live.

    An uncertain payment means the store may already be making the food.
    Asking again is how somebody pays twice, and no amount of context makes
    that a better trade than asking a person to look (§27).
    """
    return False


def describe(state: str) -> str:
    """What to say about a job in this state, when someone asks."""
    return {
        CREATED: "注文を受け付けたところだよ。",
        DRAFTING: "注文の内容を聞いているところだよ。",
        NEEDS_INFO: "確認したいことがあるよ。",
        BUILDING: "お店のカートを組み立てているところだよ。",
        AWAITING_APPROVAL: "内容を読み上げて、返事を待っているよ。",
        VERIFYING: "お店の画面と内容を突き合わせているところだよ。",
        DRY_RUN_DONE: "内容の確認まで終わったよ。決済はしていないよ。",
        PAID: "注文と支払いが完了したよ。",
        PAYMENT_UNCERTAIN: "注文が成立したか確認できていないよ。"
                           "二重注文になるといけないから、再注文は止めているよ。",
        FAILED: "注文できなかったよ。",
        ESCALATED: "お店の画面で人の操作が必要になったよ。",
        DENIED: "注文はキャンセルしたよ。",
        EXPIRED: "返事がなかったから、注文は取り消したよ。",
    }.get(state, "いまの状態がわからないよ。")


def find_active(states: list[str]) -> Optional[str]:
    """The first state in `states` that still occupies the agent, if any."""
    return next((state for state in states if is_active(state)), None)
