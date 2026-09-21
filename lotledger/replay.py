from __future__ import annotations

from decimal import Decimal

from . import lots
from .lots import LotState
from .models import Entry, Split


class ReplayResult:
    def __init__(self):
        self.lot_states: dict[str, LotState] = {}
        self.entries_by_id: dict[str, Entry] = {}
        self.splits_by_id: dict[str, Split] = {}
        self.methods: dict[tuple[str, str], str] = {}
        self.realized = Decimal("0.00")


def events_until(entries, splits, cutoff):
    events = [("entry", entry) for entry in entries]
    events.extend(("split", split) for split in splits)
    events.sort(key=lambda item: item[1].sequence)
    for kind, event in events:
        if cutoff is not None and event.timestamp > cutoff:
            break
        yield kind, event


def replay(entries, splits, cutoff=None) -> ReplayResult:
    state = ReplayResult()
    for kind, event in events_until(entries, splits, cutoff):
        if kind == "split":
            split = event
            if split.reversal_of:
                original = state.splits_by_id[split.reversal_of]
                split = Split(
                    split.id,
                    split.timestamp,
                    split.symbol,
                    original.old_shares,
                    original.new_shares,
                    split.memo,
                )
            lots.apply_split(state.lot_states, split)
            state.splits_by_id[event.id] = event
            continue

        state.entries_by_id[event.id] = event
        action = event.action
        event_type = action.get("type")
        if event_type == "buy":
            lots.open_lot(state.lot_states, state.methods, event, action)
        elif event_type == "sell":
            state.realized += lots.consume_sell(
                state.lot_states, state.methods, action
            )
        elif event_type == "reverse_buy":
            original = state.entries_by_id[action["original_entry_id"]]
            lots.reverse_buy(state.lot_states, original)
        elif event_type == "reverse_sell":
            original = state.entries_by_id[action["original_entry_id"]]
            state.realized -= lots.restore_sell(state.lot_states, original)
    return state
