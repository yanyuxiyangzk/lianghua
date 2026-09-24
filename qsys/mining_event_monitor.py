"""Read-only server-side SSE observer for workers started before progress files existed."""
import json
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo


class MiningEventStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self._round = None
        self._events = deque(maxlen=200)
        self._connected = False
        self._last_received = None

    def accept(self, event):
        kind = event.get('type')
        with self._lock:
            self._last_received = time.time()
            if kind in ('round_start', 'step_update', 'candidate_gen', 'review_result',
                        'llm_result', 'gate_eval', 'gate_pass', 'round_complete'):
                self._events.append(dict(event))
            if kind == 'round_start':
                try:
                    ts = datetime.fromisoformat(event['ts']).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
                    factor_type = event['factor_type']
                    if not isinstance(factor_type, str) or not factor_type:
                        return
                    self._round = dict(factor_type=factor_type, iteration=event.get('iteration'),
                                       observed_at=ts, status='running', source='events')
                except (ValueError, KeyError, TypeError):
                    return
            elif kind == 'round_complete':
                self._round = None
            elif kind == 'job_end' and event.get('job_key') in ('multitype_mine', 'loopengine'):
                self._round = None

    def snapshot(self, live):
        started = live.get('running', {}).get('multitype_mine')
        with self._lock:
            if (live.get('fresh') and started and self._round
                    and self._round['observed_at'] >= int(started)):
                return dict(self._round)
        return None

    def details(self):
        with self._lock:
            return dict(connected=self._connected, last_received=self._last_received,
                        events=list(self._events))

    def listen(self):
        import requests
        while True:
            try:
                # Local read-only endpoint; never triggers mining or creates a scheduler.
                with requests.get('http://127.0.0.1:8502/events', stream=True,
                                  timeout=(3, 30)) as response:
                    response.raise_for_status()
                    with self._lock:
                        self._round = None
                        self._connected = True
                    for line in response.iter_lines(chunk_size=1):
                        if line.startswith(b'data:'):
                            try:
                                self.accept(json.loads(line[5:]))
                            except (ValueError, TypeError, AttributeError):
                                continue
            except requests.RequestException:
                with self._lock:
                    self._round = None
                    self._connected = False
                threading.Event().wait(3)


def start_observer():
    observer = MiningEventStatus()
    threading.Thread(target=observer.listen, name='workflow-mining-observer', daemon=True).start()
    return observer


# Shared across the workflow and detail pages; a single stream survives navigation.
import streamlit as st


@st.cache_resource
def get_observer():
    return start_observer()
