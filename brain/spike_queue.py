from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Iterable, List, Optional

from assistant.brain.spikes import BrainSpikeEvent, spike_summary


class BrainSpikeQueue:
    """Small in-process observe-only queue for the living brain layer.

    This is intentionally side-effect light: it keeps recent brain spikes for
    inspection, tests, and future HUD wiring.  It does not execute actions.
    """

    def __init__(self, *, maxlen: int = 200) -> None:
        self._items: Deque[BrainSpikeEvent] = deque(maxlen=max(1, int(maxlen)))

    def push_many(self, spikes: Iterable[BrainSpikeEvent]) -> List[BrainSpikeEvent]:
        added: List[BrainSpikeEvent] = []
        known = {item.spike_id for item in self._items}
        for spike in list(spikes or []):
            if spike.spike_id in known:
                continue
            queued = spike.model_copy(update={"status": "queued"})
            self._items.append(queued)
            known.add(queued.spike_id)
            added.append(queued)
        return added

    def recent(self, *, limit: int = 20, turn_id: Optional[str] = None) -> List[BrainSpikeEvent]:
        items = list(reversed(self._items))
        if turn_id:
            items = [item for item in items if item.turn_id == turn_id]
        return items[: max(1, int(limit))]

    def summary(self, *, turn_id: Optional[str] = None) -> Dict[str, object]:
        items = self.recent(limit=len(self._items) or 1, turn_id=turn_id)
        return spike_summary(items)


_DEFAULT_SPIKE_QUEUE = BrainSpikeQueue()


def default_spike_queue() -> BrainSpikeQueue:
    return _DEFAULT_SPIKE_QUEUE
