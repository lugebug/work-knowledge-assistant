"""Read-only Windows WCDB adapter. Keys remain in process memory.
Layout reference: fanyuantaier/wechatauto-replica, commit 62503d76a0db069171cb4e42e87090ad285db489.
"""
from runtime_config import settings
import ctypes as C

from ctypes import wintypes as W

import hashlib, hmac, json, pathlib, re, sqlite3, struct, sys, time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096

NAME = b'com.Tencent.WCDB.Config.Cipher'

MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')

K = C.WinDLL('kernel32', use_last_error=True)

class MBI(C.Structure):
    _fields_ = [('BaseAddress',C.c_void_p),('AllocationBase',C.c_void_p),
        ('AllocationProtect',W.DWORD),('alignment1',W.DWORD),('RegionSize',C.c_size_t),
        ('State',W.DWORD),('Protect',W.DWORD),('Type',W.DWORD),('alignment2',W.DWORD)]

K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]

K.OpenProcess.restype=W.HANDLE

K.CloseHandle.argtypes=[W.HANDLE]

K.ReadProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]

K.ReadProcessMemory.restype=W.BOOL

K.VirtualQueryEx.argtypes=[W.HANDLE,C.c_void_p,C.POINTER(MBI),C.c_size_t]

K.VirtualQueryEx.restype=C.c_size_t

K.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]

K.QueryFullProcessImageNameW.restype=W.BOOL

def emit(event, **kw):
    pass  # Only the caller may record aggregate counters; never key or memory data.

def read(h,addr,n):
    if not 0x10000<=addr<0x800000000000 or n<=0 or n>4*1024*1024+512:
        return b''
    buf=C.create_string_buffer(n)
    count=C.c_size_t()
    ok=K.ReadProcessMemory(h,addr,buf,n,C.byref(count))
    return buf.raw[:count.value] if ok else b''

def search(h, pattern, deadline):
    addr=0
    hits=[]
    scanned=0
    while time.monotonic()<deadline:
        mbi=MBI()
        if not K.VirtualQueryEx(h,addr,C.byref(mbi),C.sizeof(mbi)):
            break
        base=mbi.BaseAddress or 0
        end=base+mbi.RegionSize
        if end<=addr:
            break
        if mbi.State==0x1000 and (mbi.Protect&0xff) in (2,4,8,0x20,0x40,0x80) and not(mbi.Protect&0x100):
            for offset in range(0,mbi.RegionSize,4*1024*1024):
                if time.monotonic()>deadline or scanned>4*1024**3:
                    raise TimeoutError('Bounded read-only scan limit reached')
                data=read(h,base+offset,min(4*1024*1024+256,mbi.RegionSize-offset))
                scanned+=len(data)
                hits.extend((base+offset+m.start(),m.group()) for m in pattern.finditer(data))
        addr=end
    return hits

def scan(pid):
    h=K.OpenProcess(0x0010|0x0400,False,pid)
    if not h:
        emit('process_unreadable',pid=pid,error=C.get_last_error())
        return set()
    try:
        name=C.create_unicode_buffer(32768)
        n=W.DWORD(len(name))
        if not K.QueryFullProcessImageNameW(h,0,name,C.byref(n)) or pathlib.Path(name.value).resolve()!=pathlib.Path(settings()['wechat']['executable']).resolve():
            raise RuntimeError('Unexpected process identity')
        deadline=time.monotonic()+90
        needles={addr for addr,_ in search(h,re.compile(re.escape(NAME)),deadline)}
        emit('cipher_markers',pid=pid,count=len(needles))
        if not needles:
            return set()
        pairs=[struct.pack('<QQ',addr,len(NAME)) for addr in sorted(needles)]
        refs=search(h,re.compile(b'|'.join(re.escape(p) for p in pairs)),deadline)
        out=set()
        objects=0
        for qaddr,_ in refs:
            node=read(h,qaddr-0x10,0x50)
            if len(node)<0x40 or struct.unpack_from('<Q',node,0x10)[0] not in needles:
                continue
            ptr=struct.unpack_from('<Q',node,0x28)[0]
            obj=read(h,ptr+0x88,0x28)
            if len(obj)<0x18:
                continue
            ptr,size=struct.unpack_from('<QQ',obj,8)
            if not 0<size<=1024:
                continue
            blob=read(h,ptr,size)
            if len(blob)!=size:
                continue
            objects+=1
            decoded=bytes(v^MASK[i%len(MASK)] for i,v in enumerate(blob))
            for match in re.finditer(rb"[xX]'([0-9a-fA-F]{64,192})'",decoded):
                run=match.group(1)
                starts={0}
                if len(run)>96:
                    starts.update(range(0,len(run)-63,32))
                    starts.add(len(run)-64)
                for start in starts:
                    key=bytes.fromhex(run[start:start+64].decode('ascii'))
                    if len(key)==32 and len(set(key))>=15:
                        out.add(key)
        emit('scan_complete',pid=pid,objects=objects,candidate_count=len(out))
        return out
    finally:
        K.CloseHandle(h)

def mac_key(key,page1):
    return hashlib.pbkdf2_hmac('sha512',key,bytes(b^0x3a for b in page1[:16]),2,32)

def authenticated(page,pgno,mkey):
    body=page[16 if pgno==1 else 0:PAGE-64]
    return hmac.compare_digest(hmac.new(mkey,body+struct.pack('<I',pgno),'sha512').digest(),page[-64:])

def verify_candidate(key,page):
    return authenticated(page,1,mac_key(key,page))
