from .models import State

# Navigation is entered only by Controller after all guards and the global lock.
ALLOWED = {
    State.IDLE: {State.NAVIGATING},
    State.NAVIGATING: {State.CHECKING, State.AWAITING_USER, State.SESSION_EXPIRED, State.BLOCKED, State.ERROR},
    State.CHECKING: {State.NO_SLOTS, State.POSSIBLE_SLOTS, State.AWAITING_USER,
                     State.SESSION_EXPIRED, State.BLOCKED, State.ERROR},
    State.NO_SLOTS: {State.NAVIGATING, State.IDLE, State.ERROR},
    State.POSSIBLE_SLOTS: {State.BOOKING_IN_PROGRESS, State.AWAITING_USER, State.SESSION_EXPIRED, State.BLOCKED, State.BOOKED},
    State.AWAITING_USER: {State.BOOKING_IN_PROGRESS, State.SESSION_EXPIRED, State.BLOCKED, State.BOOKED},
    State.BOOKING_IN_PROGRESS: {State.SESSION_EXPIRED, State.BLOCKED, State.BOOKED, State.AWAITING_USER},
    State.BOOKED: {State.IDLE, State.NAVIGATING},
    State.SESSION_EXPIRED: {State.IDLE, State.BOOKING_IN_PROGRESS, State.BOOKED},
    State.BLOCKED: {State.IDLE},
    State.ERROR: {State.IDLE, State.NAVIGATING, State.BOOKING_IN_PROGRESS, State.SESSION_EXPIRED, State.BLOCKED},
}


def validate_transition(old, new):
    if old != new and new not in ALLOWED[old]:
        raise ValueError(f"Invalid transition {old} -> {new}")
