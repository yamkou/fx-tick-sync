"""Bounded local resource history and memory alerts; never kills any process."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time

from .resources import load_profile, memory_severity


class ResourceObserver:
    def __init__(self, profile, probe, directory, monotonic=time.monotonic):
        self.profile, self.probe, self.monotonic = profile, probe, monotonic
        self.next_sample = None
        self.severity = None
        self.latest = None
        self.logging_failed = False
        self.anchors = {window: None for window in profile.growth_windows_seconds}
        directory = Path(directory)
        if not directory.is_dir(): raise ValueError('Existing resource log directory required')
        target = directory/'resources.jsonl'
        if any((directory/('resources.jsonl'+suffix)).is_symlink() for suffix in
               ('', *(f'.{i}' for i in range(1,profile.log_backups+1)))):
            raise ValueError('Resource logs must not redirect')
        self.handler = RotatingFileHandler(target, maxBytes=profile.log_max_bytes,
                                            backupCount=profile.log_backups, encoding='utf-8', delay=True)

    def sample_if_due(self):
        now = self.monotonic()
        if self.next_sample is not None and now < self.next_sample: return self.latest
        self.next_sample = now + self.profile.sampling_interval_seconds
        # Failed probes explicitly become UNKNOWN; monitoring must not kill collection.
        try:
            metrics = self.probe.sample()
        except Exception:
            from datetime import datetime, timezone
            from .resources import ResourceMetrics
            metrics = ResourceMetrics(datetime.now(timezone.utc).isoformat())
        severity = memory_severity(metrics, self.profile)
        transition = severity if severity != self.severity else None
        if severity is None and self.severity is not None: transition = 'RECOVERY'
        self.severity, self.latest = severity, metrics
        growth = {}
        for window, anchor in self.anchors.items():
            current = metrics.process_private_memory
            if current is None: continue
            if anchor is None:
                self.anchors[window] = (now, current)
            elif now-anchor[0] >= window:
                growth[str(window)] = {'elapsed_seconds':now-anchor[0], 'private_bytes_delta':current-anchor[1]}
                self.anchors[window] = (now, current)
        record = {'metrics':metrics.to_dict(), 'memory_severity':severity,
                  'transition':transition, 'growth':growth, 'automatic_kill':False}
        text = json.dumps(record, separators=(',',':'))
        self.logging_failed = True
        # Handler.emit normally swallows I/O errors via handleError. Surface them
        # to the collector's fixed-code health state without stopping acquisition.
        record = logging.LogRecord('resource', logging.INFO, '', 0, text, (), None)
        if self.handler.shouldRollover(record): self.handler.doRollover()
        if self.handler.stream is None: self.handler.stream = self.handler._open()
        self.handler.stream.write(text+'\n'); self.handler.stream.flush()
        self.logging_failed = False
        return metrics

    def close(self): self.handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--disk', required=True)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--duration-seconds', type=int, default=60)
    args = parser.parse_args()
    if args.duration_seconds < 1: parser.error('Positive duration required')
    from .platform.windows_resources import WindowsResourceProbe
    profile = load_profile(args.profile)
    observer = ResourceObserver(profile, WindowsResourceProbe(args.disk,args.pid), args.directory)
    stop = threading.Event()
    deadline = time.monotonic()+args.duration_seconds
    samples = 0
    try:
        while True:
            observer.sample_if_due(); samples += 1
            remaining = deadline-time.monotonic()
            if remaining <= 0: break
            stop.wait(min(profile.sampling_interval_seconds,remaining))
    finally: observer.close()
    print(json.dumps({'samples':samples,'memory_severity':observer.severity,
                      'last_metrics':observer.latest.to_dict(), 'automatic_kill':False}))


if __name__ == '__main__': main()
