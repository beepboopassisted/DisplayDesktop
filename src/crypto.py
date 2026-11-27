"""Encrypt Settings (Settings > Advanced): every data file (settings, saved desktops, places, shown
desktops, the crash journal) written encrypted with a key the user types.

File format: MAGIC (the marker: "this is a DisplayDesktop encrypted file"), a 16-byte salt, a
12-byte nonce, the 16-byte tag, then the ciphertext. AES-256-GCM (Windows' own bcrypt.dll: no
dependencies) with the MAGIC as associated data; the key comes from the typed text by
PBKDF2-HMAC-SHA256 (hashlib, ITERATIONS) with the salt. So a file is one of three kinds:
- plain JSON (encryption off, or written before it was turned on): read as before;
- ours (starts with MAGIC): read with a key that opens it (the tag proves it), else `Locked` (an
  OSError: every reader then treats it as "can't read now", never written over, and the app asks
  for the key: `on_locked`);
- anything else: damaged, as before.

Whether encryption is on comes from the files themselves (any data file of the folder starting with
MAGIC), never from a setting inside them. The key may be remembered on this PC (Settings > Remember
Key): stored in the registry protected by Windows (DPAPI: only this Windows account on this PC can
read it); otherwise it is asked for at every start (the app can't read its files without it).
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import winreg
from ctypes import POINTER, Structure, byref, c_char_p, c_ulong, c_ulonglong, c_void_p, c_wchar_p

from . import native as N

MAGIC = b"DisplayDesktop encrypted file\x00"  # the marker (the file format's number is inside: "Format")
SALT_LEN, NONCE_LEN, TAG_LEN = 16, 12, 16
HEADER_LEN = len(MAGIC) + SALT_LEN + NONCE_LEN + TAG_LEN
ITERATIONS = 200_000
KEY_VALUE = "Key"  # registry value (in datafolder.KEY): the remembered key, DPAPI-protected


class Locked(OSError):
    """Our encrypted file, and no known key opens it (wrong key, or none entered yet)."""


# ---- Windows CNG (bcrypt.dll): AES-GCM. bcrypt.dll, crypt32.dll and hashlib (OpenSSL) are loaded on
# first use: only encrypted folders and a remembered key need them (most runs never do)

_NTSTATUS = ctypes.c_long
_cng_fns: tuple | None = None


def _cng() -> tuple:
    """(Open, SetProperty, GenerateKey, Encrypt, Decrypt, DestroyKey, Close) of bcrypt.dll."""
    global _cng_fns
    if _cng_fns is None:
        b = N._load("bcrypt.dll")
        _cng_fns = (
            N._f(b, "BCryptOpenAlgorithmProvider", _NTSTATUS, POINTER(N.H), c_wchar_p, c_wchar_p, c_ulong),
            N._f(b, "BCryptSetProperty", _NTSTATUS, N.H, c_wchar_p, c_void_p, c_ulong, c_ulong),
            N._f(b, "BCryptGenerateSymmetricKey", _NTSTATUS, N.H, POINTER(N.H), c_void_p, c_ulong, c_char_p, c_ulong,
                 c_ulong),
            N._f(b, "BCryptEncrypt", _NTSTATUS, N.H, c_char_p, c_ulong, c_void_p, c_void_p, c_ulong, c_void_p, c_ulong,
                 POINTER(c_ulong), c_ulong),
            N._f(b, "BCryptDecrypt", _NTSTATUS, N.H, c_char_p, c_ulong, c_void_p, c_void_p, c_ulong, c_void_p, c_ulong,
                 POINTER(c_ulong), c_ulong),
            N._f(b, "BCryptDestroyKey", _NTSTATUS, N.H),
            N._f(b, "BCryptCloseAlgorithmProvider", _NTSTATUS, N.H, c_ulong))
    return _cng_fns


class _AuthInfo(Structure):  # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
    _fields_ = [("cbSize", c_ulong), ("dwInfoVersion", c_ulong), ("pbNonce", c_void_p), ("cbNonce", c_ulong),
                ("pbAuthData", c_void_p), ("cbAuthData", c_ulong), ("pbTag", c_void_p), ("cbTag", c_ulong),
                ("pbMacContext", c_void_p), ("cbMacContext", c_ulong), ("cbAAD", c_ulong),
                ("cbData", c_ulonglong), ("dwFlags", c_ulong)]


def _gcm(key: bytes, nonce: bytes, data: bytes, tag: bytes | None) -> tuple[bytes, bytes] | None:
    """AES-GCM: encrypts (tag None; returns (ciphertext, tag)) or decrypts (returns (plaintext, tag);
    None when the tag doesn't match: the wrong key, or not what was written)."""
    _Open, _SetProperty, _GenerateKey, _Encrypt, _Decrypt, _DestroyKey, _Close = _cng()
    alg, hkey = N.H(), N.H()
    if _Open(byref(alg), "AES", None, 0) != 0:
        raise OSError("AES unavailable")
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        if _SetProperty(alg, "ChainingMode", ctypes.addressof(mode), ctypes.sizeof(mode), 0) != 0:
            raise OSError("AES-GCM unavailable")
        if _GenerateKey(alg, byref(hkey), None, 0, key, len(key), 0) != 0:
            raise OSError("AES key refused")
        try:
            n = ctypes.create_string_buffer(nonce, len(nonce))
            a = ctypes.create_string_buffer(MAGIC, len(MAGIC))
            t = ctypes.create_string_buffer(tag or b"", TAG_LEN)
            info = _AuthInfo(ctypes.sizeof(_AuthInfo), 1, ctypes.addressof(n), len(nonce), ctypes.addressof(a),
                             len(MAGIC), ctypes.addressof(t), TAG_LEN)
            out = ctypes.create_string_buffer(max(1, len(data)))
            done = c_ulong()
            fn = _Encrypt if tag is None else _Decrypt
            status = fn(hkey, data, len(data), byref(info), None, 0, out, len(data), byref(done), 0)
            if status != 0:
                if tag is not None:
                    return None  # (STATUS_AUTH_TAG_MISMATCH: not opened by this key)
                raise OSError(f"AES-GCM failed ({status & 0xFFFFFFFF:#x})")
            return out.raw[:done.value], t.raw
        finally:
            _DestroyKey(hkey)
    finally:
        _Close(alg, 0)


# ---- state

enabled = False            # new files are written encrypted (with _key)
_key: str | None = None    # the key in use
_keys: list[str] = []      # every key entered this run (files of another folder may use another)
_derived: dict[tuple[str, bytes], bytes] = {}  # (key, salt) -> AES key (PBKDF2 is slow on purpose)
_salt: bytes | None = None  # the salt new files are written with (one per key: no PBKDF2 per write)
_lock = threading.Lock()
on_locked = None  # called with a file's path when it is ours and no known key opens it (tray: asks)


def _aes_key(key: str, salt: bytes) -> bytes:
    with _lock:
        k = _derived.get((key, salt))
        if k is None:
            import hashlib
            k = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, ITERATIONS, 32)
            _derived[(key, salt)] = k
        return k


def is_ours(raw: bytes) -> bool:
    """Starts with the marker: a DisplayDesktop encrypted file (not just some other file)."""
    return raw.startswith(MAGIC)


def encrypt(text: str, key: str | None = None) -> bytes:
    global _salt
    key = key if key is not None else _key
    if key is None:
        raise Locked("no key")
    if _salt is None or key != _key:
        salt = os.urandom(SALT_LEN)
        if key == _key:
            _salt = salt
    else:
        salt = _salt
    nonce = os.urandom(NONCE_LEN)
    ct, tag = _gcm(_aes_key(key, salt), nonce, text.encode("utf-8"), None)
    return MAGIC + salt + nonce + tag + ct


def opens(raw: bytes, key: str) -> str | None:
    """The text of our file `raw` if `key` opens it, else None."""
    if len(raw) < HEADER_LEN:
        return None
    p = len(MAGIC)
    salt, nonce, tag = raw[p:p + SALT_LEN], raw[p + SALT_LEN:p + SALT_LEN + NONCE_LEN], raw[p + SALT_LEN + NONCE_LEN:HEADER_LEN]
    got = _gcm(_aes_key(key, salt), nonce, raw[HEADER_LEN:], tag)
    if got is None:
        return None
    try:
        return got[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode(raw: bytes, path: str = "") -> str:
    """A data file's text: plain (a byte-order mark is fine), or ours opened with a known key.
    Raises Locked (and tells `on_locked`) when it is ours and no known key opens it."""
    if not is_ours(raw):
        return raw.decode("utf-8-sig")
    for key in ([_key] if _key else []) + [k for k in _keys if k != _key]:
        text = opens(raw, key)
        if text is not None:
            return text
    if on_locked is not None and path:
        try:
            on_locked(path)
        except Exception:  # noqa: BLE001 - asking must never break a read
            pass
    raise Locked(13, "encrypted: the key is needed", path)


def encode(text: str) -> bytes:
    """A data file's bytes: encrypted while encryption is on, else plain UTF-8."""
    return encrypt(text) if enabled and _key else text.encode("utf-8")


def use(key: str | None, on: bool) -> None:
    """The key in use and whether new files are written encrypted (Settings, the key popup)."""
    global _key, enabled, _salt
    if key and key not in _keys:
        _keys.append(key)
    if key != _key:
        _salt = None
    _key = key or None
    enabled = bool(on and _key)


def add_key(key: str) -> None:
    """A key entered in the popup: tried on files of any folder from now on."""
    if key and key not in _keys:
        _keys.append(key)


def key() -> str | None:
    return _key


def reset() -> None:
    """Tests: back to no encryption, no keys."""
    global enabled, _key, _salt
    enabled, _key, _salt = False, None, None
    _keys.clear()
    _derived.clear()


# ---- the files of a folder

def _data_files(folder: str) -> list[str]:
    from . import datafolder
    return [os.path.join(folder, n) for n in datafolder.FILES]


def _raw(path: str, size: int = -1) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return None


def folder_encrypted(folder: str) -> bool:
    """Any of the folder's data files is ours (encryption is on for it): only their marker's bytes are
    read (every start asks this)."""
    return any((raw := _raw(p, len(MAGIC))) is not None and is_ours(raw) for p in _data_files(folder))


def unlocks(folder: str, key: str) -> bool:
    """`key` opens every encrypted data file of the folder (and there is at least one)."""
    found = False
    for p in _data_files(folder):
        raw = _raw(p)
        if raw is not None and is_ours(raw):
            found = True
            if opens(raw, key) is None:
                return False
    return found


def recode_folder(folder: str) -> list[str]:
    """Rewrites every data file of the folder the current way (encrypted with the key in use, or
    plain): after Encrypt Settings is turned on or off or the key changes. Returns the files it could
    not rewrite (not opened by a known key, or not writable): those keep their old form."""
    from . import paths
    failed = []
    for p in _data_files(folder):
        raw = _raw(p)
        if raw is None:
            continue  # (no such file)
        try:
            text = decode(raw)  # (not `path`: no popup for a file being recoded)
            data = json.loads(text)
        except (Locked, ValueError):
            failed.append(p)
            continue
        fmt = data.get(paths.FORMAT_KEY, 1) if isinstance(data, dict) else 1
        if not (type(fmt) is int and 1 <= fmt <= paths.FORMAT):
            failed.append(p)  # (a newer version's or a bad number: damaged; the Quit / Recreate question decides)
            continue
        if not paths.write_bytes(p, encode(text)):
            failed.append(p)
    return failed


# ---- the remembered key (registry, DPAPI)

class _Blob(Structure):
    _fields_ = [("cbData", N.DWORD), ("pbData", c_void_p)]


_dpapi_fns: tuple | None = None


def _dpapi_api() -> tuple:
    """(CryptProtectData, CryptUnprotectData, LocalFree): crypt32.dll loaded on first use."""
    global _dpapi_fns
    if _dpapi_fns is None:
        c = N._load("crypt32.dll")
        _dpapi_fns = (
            N._f(c, "CryptProtectData", N.BOOL, POINTER(_Blob), c_wchar_p, c_void_p, c_void_p, c_void_p, N.DWORD,
                 POINTER(_Blob)),
            N._f(c, "CryptUnprotectData", N.BOOL, POINTER(_Blob), c_void_p, c_void_p, c_void_p, c_void_p, N.DWORD,
                 POINTER(_Blob)),
            N._f(N.kernel32, "LocalFree", c_void_p, c_void_p))
    return _dpapi_fns


CRYPTPROTECT_UI_FORBIDDEN = 0x1


# CPUID (x64): void cpuid(int leaf rcx, int subleaf rdx, int out[4] r8) - push rbx; mov eax, ecx;
# mov ecx, edx; cpuid; mov [r8], eax; mov [r8+4], ebx; mov [r8+8], ecx; mov [r8+12], edx; pop rbx; ret
_CPUID_CODE = bytes.fromhex("53 89C8 89D1 0FA2 418900 41895804 41894808 4189500C 5B C3".replace(" ", ""))
_VirtualAlloc = N._f(N.kernel32, "VirtualAlloc", c_void_p, c_void_p, ctypes.c_size_t, N.DWORD, N.DWORD)
_VirtualProtect = N._f(N.kernel32, "VirtualProtect", N.BOOL, c_void_p, ctypes.c_size_t, N.DWORD, POINTER(N.DWORD))
_VirtualFree = N._f(N.kernel32, "VirtualFree", N.BOOL, c_void_p, ctypes.c_size_t, N.DWORD)


def _cpuid_x64(leaves: list[int]) -> list[tuple[int, int, int, int]]:
    """The CPUID instruction (no Win32 API gives it): a few bytes of machine code run once."""
    mem = _VirtualAlloc(None, len(_CPUID_CODE), 0x3000, 0x04)  # MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE
    if not mem:
        raise OSError("VirtualAlloc failed")
    try:
        ctypes.memmove(mem, _CPUID_CODE, len(_CPUID_CODE))
        old = N.DWORD()
        if not _VirtualProtect(mem, len(_CPUID_CODE), 0x20, byref(old)):  # PAGE_EXECUTE_READ
            raise OSError("VirtualProtect failed")
        fn = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, POINTER(ctypes.c_uint32))(mem)
        out = []
        for leaf in leaves:
            regs = (ctypes.c_uint32 * 4)()
            fn(leaf, 0, regs)
            out.append(tuple(regs))
        return out
    finally:
        _VirtualFree(mem, 0, 0x8000)  # MEM_RELEASE


def cpu_identity() -> bytes:
    """What identifies this PC's processor (CPUID: vendor; signature and features, as Windows'
    ProcessorId; brand string; the per-core APIC id left out), or, where CPUID can't run (not x64),
    the processor description Windows keeps. Only ever used hashed (`entropy`)."""
    import platform
    import sys
    if sys.maxsize > 2 ** 32 and platform.machine().upper() in ("AMD64", "X86_64"):
        try:
            vendor, sig, b2, b3, b4 = _cpuid_x64([0, 1, 0x80000002, 0x80000003, 0x80000004])
            words = [vendor[1], vendor[3], vendor[2], sig[0], sig[3], *b2, *b3, *b4]
            return b"cpuid:" + b"".join(w.to_bytes(4, "little") for w in words)
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            parts = [str(winreg.QueryValueEx(k, n)[0]) for n in ("VendorIdentifier", "Identifier", "ProcessorNameString")]
        return ("cpu:" + "|".join(parts)).encode("utf-8")
    except OSError:
        return b"cpu:unknown"


_entropy: bytes | None = None


def entropy() -> bytes:
    """DPAPI's extra secret for the remembered key: a one-way hash (SHA-256) of this processor's
    identity, never written anywhere and not in the source: a new processor means typing the key
    again."""
    global _entropy
    if _entropy is None:
        import hashlib
        _entropy = hashlib.sha256(b"DisplayDesktop remembered key\x00" + cpu_identity()).digest()
    return _entropy


def _dpapi(data: bytes, protect: bool, entropy: bytes | None = None) -> bytes | None:
    """Windows' DPAPI: encrypted for this Windows account on this PC only (the registry never holds
    the key as text), with the processor's hash (`entropy()`) besides: a program that just asks
    DPAPI to decrypt the value for the same account gets nothing without it."""
    entropy = entropy if entropy is not None else globals()["entropy"]()
    _Protect, _Unprotect, _LocalFree = _dpapi_api()
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(entropy, len(entropy))
    src, out, extra = _Blob(len(data), ctypes.addressof(buf)), _Blob(), _Blob(len(entropy), ctypes.addressof(ent))
    ok = (_Protect(byref(src), "DisplayDesktop", byref(extra), None, None, CRYPTPROTECT_UI_FORBIDDEN, byref(out))
          if protect else
          _Unprotect(byref(src), None, byref(extra), None, None, CRYPTPROTECT_UI_FORBIDDEN, byref(out)))
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _LocalFree(out.pbData)


def stored_key() -> str | None:
    """The key remembered on this PC (Remember Key), or None. Test mode never reads it."""
    from . import datafolder, paths
    if paths.is_test_mode():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, datafolder.KEY) as k:
            value, kind = winreg.QueryValueEx(k, KEY_VALUE)
    except OSError:
        return None
    if kind != winreg.REG_BINARY or not value:
        return None
    plain = _dpapi(bytes(value), protect=False)
    try:
        return plain.decode("utf-8") if plain else None
    except UnicodeDecodeError:
        return None


def remember(key: str | None) -> bool:
    """Remembers `key` on this PC (DPAPI-protected, registry), or forgets it (None). Test mode never
    touches the registry. Returns whether it worked."""
    from . import datafolder, paths
    if paths.is_test_mode():
        return False
    try:
        if key:
            blob = _dpapi(key.encode("utf-8"), protect=True)
            if blob is None:
                return False
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, datafolder.KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, KEY_VALUE, 0, winreg.REG_BINARY, blob)
        else:
            datafolder._set_value(KEY_VALUE, None)
        return True
    except OSError:
        return False


def remembered() -> bool:
    return stored_key() is not None
