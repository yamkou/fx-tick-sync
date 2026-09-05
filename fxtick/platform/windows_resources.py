"""Read-only native Windows metrics. No WMI, credentials, services or tuning."""
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import os
import shutil

from ..resources import ResourceMetrics


class Memory(c.Structure):
    _fields_ = [('length', w.DWORD), ('load', w.DWORD)] + [(name, c.c_ulonglong) for name in
        ('total','available','page','available_page','virtual','available_virtual','extended')]


class Performance(c.Structure):
    _fields_ = [('size', w.DWORD)] + [(name, c.c_size_t) for name in
        ('commit','commit_limit','commit_peak','physical_total','physical_available',
         'system_cache','kernel_total','kernel_paged','kernel_nonpaged','page_size')] + [
        ('handles', w.DWORD), ('processes', w.DWORD), ('threads', w.DWORD)]


class ProcessMemory(c.Structure):
    _fields_ = [('size', w.DWORD), ('faults', w.DWORD)] + [(name, c.c_size_t) for name in
        ('peak_working_set','working_set','peak_paged','paged','peak_nonpaged','nonpaged',
         'pagefile','peak_pagefile','private')]


class IO(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in
        ('read_operations','write_operations','other_operations','read_bytes','write_bytes','other_bytes')]


class WindowsResourceProbe:
    def __init__(self, disk_path, pid=None):
        if os.name != 'nt': raise RuntimeError('Windows resource adapter requires Windows')
        self.disk_path, self.pid, self.previous_cpu = disk_path, pid or os.getpid(), None
        self.process_creation = None
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.psapi = c.WinDLL('psapi', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.GetProcessIoCounters.argtypes = [w.HANDLE, c.POINTER(IO)]
        self.kernel.GetSystemTimes.argtypes = [c.POINTER(w.FILETIME)] * 3
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
        self.kernel.GlobalMemoryStatusEx.argtypes = [c.POINTER(Memory)]
        self.psapi.GetPerformanceInfo.argtypes = [c.POINTER(Performance), w.DWORD]
        self.psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(ProcessMemory), w.DWORD]

    def sample(self):
        values = {'observed_at': datetime.now(timezone.utc).isoformat()}
        mem = Memory(); mem.length = c.sizeof(mem)
        if self.kernel.GlobalMemoryStatusEx(c.byref(mem)):
            values.update(total_memory=mem.total, available_memory=mem.available)
        perf = Performance(); perf.size = c.sizeof(perf)
        if self.psapi.GetPerformanceInfo(c.byref(perf), c.sizeof(perf)):
            values.update(commit_memory=perf.commit*perf.page_size,
                          commit_limit=perf.commit_limit*perf.page_size, process_count=perf.processes)
        times = [w.FILETIME() for _ in range(3)]
        if self.kernel.GetSystemTimes(*(c.byref(value) for value in times)):
            idle, kernel, user = [(v.dwHighDateTime << 32) | v.dwLowDateTime for v in times]
            current = (idle, kernel+user)
            if self.previous_cpu:
                di, dt = [v-p for v,p in zip(current, self.previous_cpu)]
                if dt > 0 and 0 <= di <= dt: values['cpu_percent'] = 100*(dt-di)/dt
            self.previous_cpu = current
        handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, self.pid)
        if handle:
            try:
                process_times = [w.FILETIME() for _ in range(4)]
                identity_ok = self.kernel.GetProcessTimes(handle, *(c.byref(v) for v in process_times))
                creation = (process_times[0].dwHighDateTime << 32) | process_times[0].dwLowDateTime
                if identity_ok and self.process_creation is None: self.process_creation = creation
                if not identity_ok or creation != self.process_creation:
                    # Never attribute a reused PID's memory to the original process.
                    return ResourceMetrics(**values)
                process = ProcessMemory(); process.size = c.sizeof(process)
                if self.psapi.GetProcessMemoryInfo(handle, c.byref(process), c.sizeof(process)):
                    values.update(process_memory=process.working_set, process_private_memory=process.private)
                io = IO()
                if self.kernel.GetProcessIoCounters(handle, c.byref(io)):
                    values.update(process_read_bytes=io.read_bytes, process_write_bytes=io.write_bytes)
            finally:
                self.kernel.CloseHandle(handle)
        try: values['disk_free'] = shutil.disk_usage(self.disk_path).free
        except OSError: pass
        return ResourceMetrics(**values)
