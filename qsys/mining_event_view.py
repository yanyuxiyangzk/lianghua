"""Accumulate round preparation separately from the bounded raw event log."""
import json


class RoundView:
    def __init__(self):
        self.previous = []
        self.reset()

    def reset(self):
        self.preparation = {}
        self.steps = {}
        self.iteration = None
        self.batch = None
        self.candidate = None
        self.ended = False
        self.current_step = None
        self.partial = True

    def update(self, events):
        keys = [json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events]
        overlap = 0
        for n in range(min(len(keys), len(self.previous)), 0, -1):
            if self.previous[-n:] == keys[:n]:
                overlap = n
                break
        if self.previous and keys and not overlap:
            self.reset()  # lost continuity; do not reuse stale preparation
        for event in events[overlap:]:
            kind = event.get('type')
            if kind == 'round_start':
                self.reset()
                self.iteration = event.get('iteration')
                self.batch = event.get('batch')
                self.partial = False
            elif kind == 'round_complete':
                self.ended = True
                self.current_step = None
            elif kind == 'step_update':
                step = event.get('step')
                if not isinstance(step, int) or not 1 <= step <= 10:
                    continue
                if step == 4:
                    # Existing workers emit step 4 once per candidate (on completion).
                    self.steps = {}
                    left = event.get('batch_left')
                    self.candidate = (self.batch - left + 1
                                      if isinstance(self.batch, int) and isinstance(left, int)
                                      else None)
                target = self.preparation if step <= 3 else self.steps
                target[step] = event
                self.current_step = step
        self.previous = keys
        return self
