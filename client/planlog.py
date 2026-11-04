"""Planning progress: which step runs, how long each took, and details.

The renderer calls `start()` once with the steps it expects, then `stage()` as
each begins and `note()` for detail (device, counts, i/n). Everything is
written to a JSON file for the studio page and echoed as plain log lines.
Calls are no-ops when no reporter is active, so library code can report freely.

A model forward pass has no real percentage, so a running step only reports
elapsed time; a heartbeat thread keeps that time moving on the page.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_current = None


class Reporter:
    def __init__(self, path, steps, clock=time.monotonic, heartbeat=0.5, echo=True):
        self.path = Path(path) if path else None
        self.clock = clock
        self.echo = echo
        self.lock = threading.RLock()
        self.t0 = clock()
        self.steps = [{'key': k, 'title': t, 'status': 'pending', 'seconds': None,
                       'detail': '', 'fraction': None} for k, t in steps]
        self.running = None
        self.started = None
        self.state = 'planning'
        self._stop = threading.Event()
        self._thread = None
        if heartbeat and self.path:
            self._thread = threading.Thread(target=self._beat, args=(heartbeat,), daemon=True)
            self._thread.start()
        self._write()

    # -- public ---------------------------------------------------------------
    def stage(self, key, title=None, detail=''):
        with self.lock:
            self._close_running('done')
            step = self._find(key, title)
            step['status'] = 'running'
            step['detail'] = detail
            step['fraction'] = None
            self.running, self.started = step, self.clock()
            self._log(f"[plan {self._index(step)}/{len(self.steps)}] {step['title']}"
                      + (f" - {detail}" if detail else ''))
            self._write()

    def note(self, detail=None, fraction=None):
        with self.lock:
            step = self.running
            if step is None:
                return
            if detail is not None:
                step['detail'] = detail
            if fraction is not None:
                step['fraction'] = max(0.0, min(1.0, float(fraction)))
            self._write()

    def skip(self, key, detail=''):
        with self.lock:
            step = self._find(key, None)
            if step['status'] == 'pending':
                step['status'] = 'skipped'
                step['detail'] = detail
                self._write()

    def finish(self, ok=True, error=''):
        with self.lock:
            self._close_running('done' if ok else 'failed')
            for step in self.steps:
                if step['status'] == 'pending':
                    step['status'] = 'skipped'
            self.state = 'done' if ok else 'failed'
            self._error = error
            self._log(f"[plan] {'finished' if ok else 'failed'} in {self.clock() - self.t0:.1f}s")
            self._write()
        self._stop.set()

    # -- internals ------------------------------------------------------------
    def _find(self, key, title):
        for step in self.steps:
            if step['key'] == key:
                if title:
                    step['title'] = title
                return step
        step = {'key': key, 'title': title or key, 'status': 'pending', 'seconds': None,
                'detail': '', 'fraction': None}
        self.steps.append(step)
        return step

    def _index(self, step):
        return self.steps.index(step) + 1

    def _close_running(self, status):
        if self.running is not None:
            self.running['status'] = status
            self.running['seconds'] = round(self.clock() - self.started, 2)
            self.running['fraction'] = 1.0 if status == 'done' else self.running['fraction']
            self._log(f"[plan]   {self.running['title']}: {status} {self.running['seconds']:.1f}s")
            self.running = None

    def _log(self, line):
        if self.echo:
            print(line, flush=True)

    def _beat(self, interval):
        while not self._stop.wait(interval):
            with self.lock:
                if self.running is not None:
                    self._write()

    def snapshot(self):
        now = self.clock()
        steps = []
        for step in self.steps:
            item = dict(step)
            if step is self.running:
                item['seconds'] = round(now - self.started, 2)
            steps.append(item)
        done = sum(1 for s in steps if s['status'] in ('done', 'skipped'))
        part = 0.0
        if self.running is not None and self.running['fraction'] is not None:
            part = self.running['fraction']
        total = max(1, len(steps))
        return {'kind': 'plan', 'state': self.state, 'elapsed': round(now - self.t0, 2),
                'steps': steps, 'fraction': round(min(1.0, (done + part) / total), 4),
                'error': getattr(self, '_error', '')}

    def _write(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding='utf-8')
            os.replace(str(tmp), str(self.path))
        except OSError:
            pass


def start(path, steps, **kwargs):
    global _current
    _current = Reporter(path, steps, **kwargs)
    return _current


def stage(key, title=None, detail=''):
    if _current is not None:
        _current.stage(key, title, detail)


def note(detail=None, fraction=None):
    if _current is not None:
        _current.note(detail, fraction)


def skip(key, detail=''):
    if _current is not None:
        _current.skip(key, detail)


def finish(ok=True, error=''):
    global _current
    if _current is not None:
        _current.finish(ok, error)
        _current = None
