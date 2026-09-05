"""Explicit, minimized, staggered MT5 startup. Plan-only unless --start is given.

No autologon/task/service registration, login, process kill or machine tuning.
Installation and portable Data Folder acceptance must precede actual use.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import threading

from ..config import load_config, native_path
from ..resources import load_profile, memory_severity
from .windows_resources import WindowsResourceProbe


def running_executables():
    kernel, psapi = c.WinDLL('kernel32',use_last_error=True), c.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]; kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
    psapi.EnumProcesses.argtypes=[c.POINTER(w.DWORD),w.DWORD,c.POINTER(w.DWORD)]
    entries=(w.DWORD*32768)(); used=w.DWORD()
    if not psapi.EnumProcesses(entries,c.sizeof(entries),c.byref(used)) or used.value>=c.sizeof(entries):
        raise RuntimeError('Process inventory unavailable')
    result=set()
    for pid in entries[:used.value//c.sizeof(w.DWORD)]:
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: continue
        try:
            buffer=c.create_unicode_buffer(32768); size=w.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle,0,buffer,c.byref(size)):
                result.add(os.path.normcase(str(Path(buffer.value).resolve())))
        finally: kernel.CloseHandle(handle)
    return result


def selected_terminals(config_path, count):
    config=load_config(config_path)
    terminals=sorted(config.terminals,key=lambda value:value.terminal_id)[:count]
    if not 1<=count<=10 or len(terminals)!=count: raise ValueError('Invalid terminal count')
    selected=[(terminal.terminal_id,native_path(terminal.path,Path(config_path).resolve().parent))
              for terminal in terminals]
    if len({os.path.normcase(str(path.parent)) for _,path in selected})!=count:
        raise ValueError('Portable terminals must have distinct directories')
    if any(path.name.lower()!='terminal64.exe' for _,path in selected):
        raise ValueError('Expected reviewed MT5 executable')
    return selected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True); parser.add_argument('--profile',required=True)
    parser.add_argument('--count',type=int,default=1); parser.add_argument('--start',action='store_true')
    args=parser.parse_args()
    selected=selected_terminals(args.config,args.count); profile=load_profile(args.profile)
    if not args.start:
        print(json.dumps({'mode':'plan-only','terminal_ids':[name for name,_ in selected],
                          'portable':True,'minimized':True,'delay_seconds':profile.startup_delay_seconds}))
        return 0
    if os.name!='nt' or any(not path.is_file() for _,path in selected):
        raise RuntimeError('Verified Windows installations required before startup')
    stop=threading.Event(); started=[]
    for index,(name,path) in enumerate(selected):
        metrics=WindowsResourceProbe(path.parent).sample()
        if memory_severity(metrics,profile) is not None:
            print(json.dumps({'started':started,'blocked':'memory-budget-or-unknown','automatic_kill':False}))
            return 2
        if os.path.normcase(str(path)) in running_executables():
            print(json.dumps({'terminal_id':name,'status':'already-running'})); continue
        startup=subprocess.STARTUPINFO(); startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=6  # SW_MINIMIZE; explicit user-requested GUI app.
        child=subprocess.Popen([str(path),'/portable'],cwd=path.parent,startupinfo=startup)
        started.append({'terminal_id':name,'pid':child.pid})
        if index<len(selected)-1: stop.wait(profile.startup_delay_seconds)
    print(json.dumps({'started':started,'automatic_kill':False,'acceptance_test_required':True}))
    return 0


if __name__=='__main__': raise SystemExit(main())
