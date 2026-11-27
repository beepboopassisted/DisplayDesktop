"""GPU layers for the slide animation (DirectComposition, built into Windows 8+; ctypes, no packages).

Without it, every animation frame copies the old and the new desktop picture into the overlay window
(two full-monitor BitBlts per frame on the CPU). With it, each
picture is copied to the GPU once (a composition surface) and a frame only moves the pictures
(IDCompositionVisual::SetOffsetX + Commit: microseconds of CPU). The pictures are put on the window by
the compositor (DWM), above the window's own content, and they follow its display affinity, so a
window left out of screen captures also leaves its GPU pictures out.

Which GPU: always the one that drives the monitor being animated (`gpu_of`: the DXGI adapter whose
output is that monitor). One D3D11 + DirectComposition device per GPU (`devices_for`, made on first
use), shared by that GPU's overlays and captures. The GPUs are the DXGI adapters that the kernel lists
as able to render: software renderers and indirect/virtual display adapters are left out (a monitor
on one of those: the CPU draws). If anything fails (no GPU driver, device lost), callers fall back to
the CPU drawing, and that GPU's devices are dropped so a later animation tries again.
`ENABLED = False` forces the CPU drawing: the "Use GPU Acceleration" setting (`set_enabled`), and tests.

The screen on the GPU (Capture, Desktop Duplication): each monitor's picture is taken on its GPU,
with no copy through memory (a 4K screenshot through memory takes ~50 ms; a frame here ~0.1 ms). The
slide's old desktop is the latest frame and its new desktop is refreshed every frame while it slides.
Kept open between slides (warm_captures: at start, when the setting turns on, after a display change).

Vtable slots were checked on screen: IDCompositionVisual::SetOffsetX(float) is slot 4; slot 3 is
the IDCompositionAnimation overload (MSVC orders overloads in reverse).
"""
from __future__ import annotations

import ctypes
import time
from collections import namedtuple
from ctypes import POINTER, WINFUNCTYPE, Structure, byref, c_float, c_long, c_uint, c_ulong, c_void_p, c_wchar

from . import gui as G
from . import native as N
from .shortcut import GUID

ENABLED = True

IID_IDXGIDevice = "{54ec77fa-1377-44e6-8c32-88fd5f44c84c}"
IID_IDCompositionDevice = "{C37EA93A-E7AA-450D-B16F-9746CB0407F3}"
IID_IDXGISurface1 = "{4AE63092-6327-4c1b-80AE-BFE12EA32B86}"
D3D_DRIVER_TYPE_UNKNOWN = 0  # (the adapter is passed: the driver type must be "unknown")
IID_IDXGIFactory1 = "{770aae78-f26f-4dba-a829-253c83d1b387}"
DXGI_ADAPTER_FLAG_SOFTWARE = 2
VENDOR_MICROSOFT = 0x1414
_FACTORY_ENUMADAPTERS1, _ADAPTER_GETDESC1 = 12, 10
KMTQAITYPE_ADAPTERTYPE = 15
_KMT_RENDER, _KMT_SOFTWARE, _KMT_INDIRECT = 0x1, 0x4, 0x40
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_ALPHA_MODE_IGNORE = 3

# vtable slots (IUnknown: 0 QueryInterface, 1 AddRef, 2 Release)
_QI, _RELEASE = 0, 2
_DEV_COMMIT, _DEV_WAIT, _DEV_TARGET, _DEV_VISUAL, _DEV_SURFACE = 3, 4, 6, 7, 8
_TARGET_SETROOT = 3
_VIS_OFFSETX, _VIS_CONTENT, _VIS_ADD = 4, 15, 16
_SURF_BEGIN, _SURF_END = 3, 4
_DXGISURF_GETDC, _DXGISURF_RELEASEDC = 11, 12
# Desktop Duplication (Capture): IDXGIAdapter::EnumOutputs 7, IDXGIOutput::GetDesc 7,
# IDXGIOutput5::DuplicateOutput1 26, IDXGIOutputDuplication AcquireNextFrame 8 / ReleaseFrame 14,
# ID3D11Device::CreateTexture2D 5, ID3D11DeviceContext Map 14 / Unmap 15 / CopySubresourceRegion 46 /
# CopyResource 47, ID3D11Texture2D::GetDesc 10
_ADAPTER_ENUMOUTPUTS, _OUTPUT_GETDESC, _OUTPUT5_DUPLICATE1 = 7, 7, 26
_DUP_ACQUIRE, _DUP_RELEASEFRAME = 8, 14
_D3D_CREATETEXTURE2D = 5
_CTX_MAP, _CTX_UNMAP, _CTX_COPYSUBRESOURCEREGION, _CTX_COPYRESOURCE = 14, 15, 46, 47
IID_IDXGIOutput5 = "{80A07424-AB52-42EB-833C-0C42FD282D98}"
IID_ID3D11Texture2D = "{6f15aaf2-d208-4e89-9ab4-489535d34f9c}"
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_MODE_ROTATION_IDENTITY = 1
D3D11_USAGE_STAGING, D3D11_CPU_ACCESS_READ, D3D11_MAP_READ = 3, 0x20000, 1

try:
    _d3d11 = N._load("d3d11.dll")
    _dcomp = N._load("dcomp.dll")
    _D3D11CreateDevice = N._f(_d3d11, "D3D11CreateDevice", c_long, c_void_p, c_uint, c_void_p, c_uint, c_void_p,
                              c_uint, c_uint, POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p))
    _DCompositionCreateDevice = N._f(_dcomp, "DCompositionCreateDevice", c_long, c_void_p, POINTER(GUID),
                                     POINTER(c_void_p))
except (OSError, AttributeError):  # Windows without DirectComposition: CPU drawing only
    _D3D11CreateDevice = _DCompositionCreateDevice = None


class _LUID(Structure):
    _fields_ = [("Low", c_ulong), ("High", c_long)]

    @property
    def value(self) -> int:
        return ((self.High & 0xFFFFFFFF) << 32) | self.Low


class _DESC1(Structure):
    _fields_ = [("Description", c_wchar * 128), ("VendorId", c_uint), ("DeviceId", c_uint), ("SubSysId", c_uint),
                ("Revision", c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _LUID), ("Flags", c_uint)]


class _KMT_ADAPTERINFO(Structure):
    _fields_ = [("hAdapter", c_uint), ("AdapterLuid", _LUID), ("NumOfSources", c_ulong),
                ("bPrecisePresentRegionsPreferred", c_long)]


class _KMT_ENUMADAPTERS2(Structure):
    _fields_ = [("NumAdapters", c_ulong), ("pAdapters", POINTER(_KMT_ADAPTERINFO))]


class _KMT_QUERYADAPTERINFO(Structure):
    _fields_ = [("hAdapter", c_uint), ("Type", c_uint), ("pPrivateDriverData", c_void_p),
                ("PrivateDriverDataSize", c_uint)]


class _KMT_CLOSEADAPTER(Structure):
    _fields_ = [("hAdapter", c_uint)]


def _optional(dll, name: str, restype, *argtypes):
    """A DLL function, or None if it (or the DLL: a System32 file name, else a loaded one) is missing.
    (One DLL object per DLL: each costs a function-pointer class.)"""
    try:
        return N._f(N._load(dll) if isinstance(dll, str) else dll, name, restype, *argtypes)
    except (OSError, AttributeError):
        return None


_CreateDXGIFactory1 = _optional("dxgi.dll", "CreateDXGIFactory1", c_long, POINTER(GUID), POINTER(c_void_p))
_KMTEnumAdapters2 = _optional(N.gdi32, "D3DKMTEnumAdapters2", c_long, POINTER(_KMT_ENUMADAPTERS2))
_KMTQueryAdapterInfo = _optional(N.gdi32, "D3DKMTQueryAdapterInfo", c_long, POINTER(_KMT_QUERYADAPTERINFO))
_KMTCloseAdapter = _optional(N.gdi32, "D3DKMTCloseAdapter", c_long, POINTER(_KMT_CLOSEADAPTER))


# name: as Windows names it (the adapter's description); key: stable across restarts:
# vendor-device-subsystem-revision[#n]; luid: this boot's id (device creation, outputs)
Adapter = namedtuple("Adapter", "name key luid")


def _kernel_types() -> dict[int, int] | None:
    """LUID -> kernel adapter type flags (None if the kernel can't be asked)."""
    if not (_KMTEnumAdapters2 and _KMTQueryAdapterInfo and _KMTCloseAdapter):
        return None
    e = _KMT_ENUMADAPTERS2()
    if _KMTEnumAdapters2(byref(e)) < 0:
        return None
    arr = (_KMT_ADAPTERINFO * max(1, e.NumAdapters))()
    e.pAdapters = ctypes.cast(arr, POINTER(_KMT_ADAPTERINFO))
    if _KMTEnumAdapters2(byref(e)) < 0:
        return None
    types = {}
    for k in range(e.NumAdapters):
        info = arr[k]
        flags = c_uint()
        q = _KMT_QUERYADAPTERINFO(info.hAdapter, KMTQAITYPE_ADAPTERTYPE, ctypes.cast(byref(flags), c_void_p),
                                  ctypes.sizeof(flags))
        if _KMTQueryAdapterInfo(byref(q)) >= 0:
            types[info.AdapterLuid.value] = flags.value
        _KMTCloseAdapter(byref(_KMT_CLOSEADAPTER(info.hAdapter)))
    return types


def _factory() -> c_void_p:
    f = c_void_p()
    if _CreateDXGIFactory1 is None or _CreateDXGIFactory1(byref(GUID.of(IID_IDXGIFactory1)), byref(f)) < 0 or not f:
        raise OSError("DXGI not available")
    return f


def _each_adapter(factory: c_void_p):
    """(IDXGIAdapter1, DXGI_ADAPTER_DESC1) for every DXGI adapter; the caller releases the adapter."""
    i = 0
    while True:
        a = c_void_p()
        vtbl = ctypes.cast(factory, POINTER(POINTER(c_void_p)))[0]
        hr = WINFUNCTYPE(c_long, c_void_p, c_uint, POINTER(c_void_p))(vtbl[_FACTORY_ENUMADAPTERS1])(factory, i, byref(a))
        if hr < 0 or not a:
            return
        d = _DESC1()
        try:
            _call(a, _ADAPTER_GETDESC1, (POINTER(_DESC1), byref(d)))
        except OSError:
            _release(a)
            i += 1
            continue
        yield a, d
        i += 1


def adapters() -> list[Adapter]:
    """The GPUs that can draw (in Windows' order). Empty if none or DXGI is missing."""
    try:
        factory = _factory()
    except OSError:
        return []
    kernel = _kernel_types()
    found: list[Adapter] = []
    seen: dict[str, int] = {}
    try:
        for a, d in _each_adapter(factory):
            _release(a)
            luid = d.AdapterLuid.value
            if d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE or d.VendorId == VENDOR_MICROSOFT:
                continue
            kt = kernel.get(luid) if kernel is not None else None
            if kernel is not None and (kt is None or not kt & _KMT_RENDER or kt & (_KMT_SOFTWARE | _KMT_INDIRECT)):
                continue  # not a GPU that renders (e.g. a virtual display borrowing a GPU's name)
            if any(x.luid == luid for x in found):
                continue
            base = f"{d.VendorId:04X}-{d.DeviceId:04X}-{d.SubSysId:08X}-{d.Revision:02X}"
            seen[base] = seen.get(base, 0) + 1
            key = base if seen[base] == 1 else f"{base}#{seen[base]}"
            found.append(Adapter(d.Description.strip(), key, luid))
    finally:
        _release(factory)
    return found


def _outputs_of(adapter: c_void_p):
    """(IDXGIOutput, DXGI_OUTPUT_DESC) of each output (monitor) an adapter drives; the caller
    releases each output."""
    i = 0
    while True:
        o = c_void_p()
        if _vcall(adapter, _ADAPTER_ENUMOUTPUTS, c_long, (c_uint, i), (POINTER(c_void_p), byref(o))) < 0 or not o:
            return
        desc = _OUTDESC()
        try:
            _call(o, _OUTPUT_GETDESC, (POINTER(_OUTDESC), byref(desc)))
        except OSError:
            _release(o)
            i += 1
            continue
        yield o, desc
        i += 1


def _monitor_gpus() -> dict[str, int]:
    """Monitor device name (lower case) -> the LUID of the GPU that drives it."""
    out: dict[str, int] = {}
    try:
        factory = _factory()
    except OSError:
        return out
    try:
        for a, d in _each_adapter(factory):
            try:
                for o, desc in _outputs_of(a):
                    out.setdefault(desc.DeviceName.lower(), d.AdapterLuid.value)
                    _release(o)
            finally:
                _release(a)
    finally:
        _release(factory)
    return out


_gpu_of: dict[str, Adapter | None] = {}  # monitor -> the GPU that drives it (forgotten on a display change)


def gpu_of(device_name: str) -> Adapter | None:
    """The GPU that drives a monitor (None: none that can draw, e.g. a virtual display: the CPU)."""
    key = device_name.lower()
    if key not in _gpu_of:
        luid = _monitor_gpus().get(key)
        _gpu_of[key] = next((g for g in adapters() if g.luid == luid), None) if luid is not None else None
    return _gpu_of[key]


def _call(obj: c_void_p, slot: int, *args: tuple) -> int:
    """COM method `slot` of `obj` with (ctype, value) args; raises OSError on a failed HRESULT, and on
    an object already released (NULL: a GPU failure for the caller, never an app error)."""
    if not obj:
        raise OSError(f"GPU object released (slot {slot})")
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    hr = WINFUNCTYPE(c_long, c_void_p, *(t for t, _ in args))(vtbl[slot])(obj, *(v for _, v in args))
    if hr < 0:
        raise OSError(f"GPU call failed (slot {slot}, 0x{hr & 0xFFFFFFFF:08X})")
    return hr


def _vcall(obj: c_void_p, slot: int, restype, *args: tuple):
    """A COM method that returns no HRESULT (void, or a plain value). OSError on a released object."""
    if not obj:
        raise OSError(f"GPU object released (slot {slot})")
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    return WINFUNCTYPE(restype, c_void_p, *(t for t, _ in args))(vtbl[slot])(obj, *(v for _, v in args))


def _release(obj: c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[_RELEASE])(obj)


class _Devices:
    """The D3D11 + DirectComposition devices on one GPU. Layers made on it keep it alive: retire()
    (when it stops being the shared device) closes it only once its last layer is closed, so a
    slide still running on it never uses a released device."""

    def __init__(self, gpu: Adapter):
        self.gpu = gpu
        self.layers = 0
        self.retired = False
        self.d3d, self.context, self.dxgi, self.dcomp = c_void_p(), c_void_p(), c_void_p(), c_void_p()
        level = c_uint()
        try:
            factory = _factory()
            adapter = c_void_p()
            try:
                for a, d in _each_adapter(factory):
                    if d.AdapterLuid.value == gpu.luid and not adapter:
                        adapter = a
                    else:
                        _release(a)
                if not adapter:
                    raise OSError(f"GPU not found: {gpu.name}")
                hr = _D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                        None, 0, D3D11_SDK_VERSION, byref(self.d3d), byref(level),
                                        byref(self.context))
            finally:
                _release(adapter)
                _release(factory)
            if hr < 0 or not self.d3d:
                raise OSError(f"no Direct3D device on {gpu.name}")
            _call(self.d3d, _QI, (POINTER(GUID), byref(GUID.of(IID_IDXGIDevice))), (POINTER(c_void_p), byref(self.dxgi)))
            if _DCompositionCreateDevice(self.dxgi, byref(GUID.of(IID_IDCompositionDevice)), byref(self.dcomp)) < 0 \
                    or not self.dcomp:
                raise OSError("no DirectComposition device")
        except BaseException:
            self.close()
            raise

    def retire(self) -> None:
        self.retired = True
        if self.layers == 0:
            self.close()

    def close(self) -> None:
        for name in ("dcomp", "dxgi", "context", "d3d"):
            _release(getattr(self, name))
            setattr(self, name, c_void_p())


_devices: dict[int, _Devices] = {}  # GPU LUID -> its devices (one per GPU, made on first use)
RETRY_AFTER_S = 60.0   # a GPU device or layer that failed is not tried again for this long
_failed_at: dict[int, float] = {}  # GPU LUID -> time.monotonic() of its last failed device or layer


def devices_for(device_name: str) -> _Devices | None:
    """The devices on the GPU that drives a monitor, made on first use (None: the GPU is off, the
    monitor has no GPU of its own, or that GPU failed less than RETRY_AFTER_S ago: the CPU draws, with
    no new try between a switch's screenshot and hiding the windows)."""
    if not ENABLED or _D3D11CreateDevice is None:
        return None
    gpu = gpu_of(device_name)
    if gpu is None:
        return None
    dev = _devices.get(gpu.luid)
    if dev is None:
        failed = _failed_at.get(gpu.luid)
        if failed is not None and time.monotonic() - failed < RETRY_AFTER_S:
            return None
        try:
            dev = _devices[gpu.luid] = _Devices(gpu)
        except (OSError, ValueError):
            _failed_at[gpu.luid] = time.monotonic()
            return None
        _failed_at.pop(gpu.luid, None)
    return dev


def available(device_name: str | None = None) -> bool:
    """True if animations on that monitor (None: on any monitor) can use the GPU."""
    names = [device_name] if device_name is not None else [s.device for s in N.screens()]
    return any(devices_for(n) is not None for n in names)


def describe() -> str:
    """Where the animations run, for the log: each GPU used by a monitor, or the CPU."""
    used = list(dict.fromkeys(d.gpu.name for d in (devices_for(s.device) for s in N.screens()) if d))
    return ("the GPU " + ", ".join(used)) if used else "the CPU"


def set_enabled(on: bool) -> bool:
    """The "Use GPU Acceleration" setting. Off: every GPU device is released and animations draw on
    the CPU. On: each monitor's GPU devices and capture are made now (never during the first slide).
    Returns whether animations now run on a GPU."""
    global ENABLED
    ENABLED = bool(on)
    if not ENABLED:
        reset()
    _failed_at.clear()
    on_gpu = available()
    warm_captures()
    return on_gpu


def layer_failed(device: "_Devices | None") -> None:
    """A slide could not put its pictures on that GPU (making its layer failed): its devices are let
    go and that GPU is not tried again for RETRY_AFTER_S."""
    if device is not None:
        _failed_at[device.gpu.luid] = time.monotonic()
        reset(device)


def reset(device: "_Devices | None" = None) -> None:
    """Stops using GPU devices (after a failure: only `device`, if it is still the one in use for its
    GPU; a settings or display change: all, with every capture). A device is closed once no layer or
    capture uses it any more."""
    if device is None:
        drop_captures()
        for old in list(_devices.values()):
            old.retire()
        _devices.clear()
        return
    if _devices.get(device.gpu.luid) is device:
        del _devices[device.gpu.luid]
        device.retire()


class Layer:
    """GPU pictures on top of one window. Each picture is a (dc, width, height) GDI source, copied to
    a composition surface once; set_offsets moves them; commit hands the frame to the compositor."""

    def __init__(self, hwnd: int, devices: "_Devices | None" = None):
        """`devices`: a GPU's (a Capture's: its pictures are textures there); else those of the GPU that
        drives the window's monitor."""
        if devices is None:
            name = N.monitor_device_of_window(hwnd)
            devices = devices_for(name) if name is not None else None
        dev = devices
        if dev is None:
            raise OSError("GPU not available")
        self._owner: _Devices | None = dev  # kept alive while this layer exists (see _Devices)
        dev.layers += 1
        self._dev = dev.dcomp
        self._target, self._root = c_void_p(), c_void_p()
        self._visuals: list[c_void_p] = []
        self._surfaces: list[c_void_p] = []
        try:
            _call(self._dev, _DEV_TARGET, (N.H, hwnd), (ctypes.c_int, 1), (POINTER(c_void_p), byref(self._target)))
            _call(self._dev, _DEV_VISUAL, (POINTER(c_void_p), byref(self._root)))
            _call(self._target, _TARGET_SETROOT, (c_void_p, self._root))
        except BaseException:
            self.close()
            raise

    def add(self, src_dc: int, w: int, h: int, x: int = 0) -> int:
        """Adds a picture (on top of the others) at offset x; returns its index."""
        surface, visual = c_void_p(), c_void_p()
        try:
            _call(self._dev, _DEV_SURFACE, (c_uint, w), (c_uint, h), (c_uint, DXGI_FORMAT_B8G8R8A8_UNORM),
                  (c_uint, DXGI_ALPHA_MODE_IGNORE), (POINTER(c_void_p), byref(surface)))
            dxgi_surface, at = c_void_p(), N.POINT()
            _call(surface, _SURF_BEGIN, (c_void_p, None), (POINTER(GUID), byref(GUID.of(IID_IDXGISurface1))),
                  (POINTER(c_void_p), byref(dxgi_surface)), (POINTER(N.POINT), byref(at)))
            try:
                hdc = N.H()
                _call(dxgi_surface, _DXGISURF_GETDC, (ctypes.c_int, 0), (POINTER(N.H), byref(hdc)))
                try:
                    # the surface may be part of a larger atlas: draw at the offset it reports
                    G.BitBlt(hdc.value, at.x, at.y, w, h, src_dc, 0, 0, G.SRCCOPY)
                finally:
                    _call(dxgi_surface, _DXGISURF_RELEASEDC, (c_void_p, None))
            finally:
                _release(dxgi_surface)
                _call(surface, _SURF_END)
            _call(self._dev, _DEV_VISUAL, (POINTER(c_void_p), byref(visual)))
            _call(visual, _VIS_CONTENT, (c_void_p, surface))
            _call(visual, _VIS_OFFSETX, (c_float, float(x)))
            _call(self._root, _VIS_ADD, (c_void_p, visual), (ctypes.c_int, 1), (c_void_p, None))
        except BaseException:
            _release(visual)
            _release(surface)
            raise
        self._visuals.append(visual)
        self._surfaces.append(surface)
        return len(self._visuals) - 1

    def add_texture(self, texture: c_void_p, w: int, h: int, x: int = 0) -> int:
        """Adds a picture copied from a texture on this layer's GPU (no CPU copy); returns its index."""
        surface, visual = c_void_p(), c_void_p()
        try:
            _call(self._dev, _DEV_SURFACE, (c_uint, w), (c_uint, h), (c_uint, DXGI_FORMAT_B8G8R8A8_UNORM),
                  (c_uint, DXGI_ALPHA_MODE_IGNORE), (POINTER(c_void_p), byref(surface)))
            self._fill(surface, texture)
            _call(self._dev, _DEV_VISUAL, (POINTER(c_void_p), byref(visual)))
            _call(visual, _VIS_CONTENT, (c_void_p, surface))
            _call(visual, _VIS_OFFSETX, (c_float, float(x)))
            _call(self._root, _VIS_ADD, (c_void_p, visual), (ctypes.c_int, 1), (c_void_p, None))
        except BaseException:
            _release(visual)
            _release(surface)
            raise
        self._visuals.append(visual)
        self._surfaces.append(surface)
        return len(self._visuals) - 1

    def set_texture(self, index: int, texture: c_void_p) -> None:
        """A picture's content again, from a texture (a live picture updated during the slide)."""
        self._fill(self._surfaces[index], texture)

    def _fill(self, surface: c_void_p, texture: c_void_p) -> None:
        if not texture:
            raise OSError("GPU picture released")
        dst, at = c_void_p(), N.POINT()
        _call(surface, _SURF_BEGIN, (c_void_p, None), (POINTER(GUID), byref(GUID.of(IID_ID3D11Texture2D))),
              (POINTER(c_void_p), byref(dst)), (POINTER(N.POINT), byref(at)))
        try:
            # (the surface may be part of a larger atlas: copied to the offset it reports)
            _vcall(self._owner.context, _CTX_COPYSUBRESOURCEREGION, None, (c_void_p, dst), (c_uint, 0),
                   (c_uint, at.x), (c_uint, at.y), (c_uint, 0), (c_void_p, texture), (c_uint, 0), (c_void_p, None))
        finally:
            _release(dst)
            _call(surface, _SURF_END)

    def set_offsets(self, xs: list[int]) -> None:
        for visual, x in zip(self._visuals, xs):
            _call(visual, _VIS_OFFSETX, (c_float, float(x)))

    def commit(self, wait: bool = False) -> None:
        """Hands the changes to the compositor; wait=True blocks until it has taken them."""
        _call(self._dev, _DEV_COMMIT)
        if wait:
            _call(self._dev, _DEV_WAIT)

    @property
    def count(self) -> int:
        return len(self._visuals)

    @property
    def device(self) -> "_Devices | None":
        """The devices this layer was made on (for reset(device) after a failure)."""
        return self._owner

    def close(self) -> None:
        for v in self._visuals:
            _release(v)
        for s in self._surfaces:
            _release(s)
        self._visuals, self._surfaces = [], []
        _release(self._root)
        _release(self._target)
        self._root, self._target = c_void_p(), c_void_p()
        owner, self._owner = self._owner, None
        if owner is not None:
            owner.layers -= 1
            if owner.retired and owner.layers == 0:
                owner.close()


# ---------------------------------------------------------------- the screen, captured on the GPU

class _OUTDESC(Structure):
    _fields_ = [("DeviceName", c_wchar * 32), ("Desktop", N.RECT), ("Attached", c_long), ("Rotation", c_uint),
                ("Monitor", c_void_p)]


class _TEXDESC(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleCount", c_uint), ("SampleQuality", c_uint), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class _FRAMEINFO(Structure):
    _fields_ = [("LastPresentTime", ctypes.c_int64), ("LastMouseUpdateTime", ctypes.c_int64),
                ("AccumulatedFrames", c_uint), ("RectsCoalesced", c_long), ("ProtectedContentMaskedOut", c_long),
                ("PointerX", c_long), ("PointerY", c_long), ("PointerVisible", c_long),
                ("TotalMetadataBufferSize", c_uint), ("PointerShapeBufferSize", c_uint)]


class _MAPPED(Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


_QueryPerformanceCounter = N._f(N.kernel32, "QueryPerformanceCounter", c_long, POINTER(ctypes.c_int64))


def qpc() -> int:
    """Now, in the ticks of a captured frame's present time."""
    t = ctypes.c_int64()
    _QueryPerformanceCounter(byref(t))
    return t.value


def _texture_desc(texture: c_void_p) -> _TEXDESC:
    d = _TEXDESC()
    _vcall(texture, 10, None, (POINTER(_TEXDESC), byref(d)))
    return d


class Capture:
    """One monitor's picture, captured on the GPU that drives it (Desktop Duplication): a frame
    costs no CPU copy (about 0.1 ms, against ~50 ms for a 4K screenshot through memory), and it
    leaves out windows excluded from capture (the slide layer). `texture` holds the latest frame
    (`grab`), `present` its present time (qpc ticks). Kept open between slides; anything failing
    raises OSError (the caller drops it: drop_capture)."""

    def __init__(self, device_name: str):
        self.device_name = device_name
        self.devices: _Devices | None = None
        self.users = 0  # slides using it now (`use`/`done`): a dropped capture closes after the last
        self.retired = False
        self.texture, self._dup, self._old = c_void_p(), c_void_p(), c_void_p()
        self.w = self.h = 0
        self.present = 0
        output, luid = self._find_output(device_name)
        try:
            devices = devices_for(device_name)
            if devices is None or devices.gpu.luid != luid:
                raise OSError("no GPU devices for this monitor")
            self.devices = devices
            self.devices.layers += 1  # (kept alive while this capture exists)
            out5 = c_void_p()
            _call(output, _QI, (POINTER(GUID), byref(GUID.of(IID_IDXGIOutput5))), (POINTER(c_void_p), byref(out5)))
            try:
                formats = (c_uint * 1)(DXGI_FORMAT_B8G8R8A8_UNORM)
                _call(out5, _OUTPUT5_DUPLICATE1, (c_void_p, self.devices.d3d), (c_uint, 0), (c_uint, 1),
                      (POINTER(c_uint), formats), (POINTER(c_void_p), byref(self._dup)))
            finally:
                _release(out5)
        except BaseException:
            self.close()
            raise
        finally:
            _release(output)

    @staticmethod
    def _find_output(device_name: str) -> tuple[c_void_p, int]:
        """The DXGI output showing that monitor (not rotated), and its GPU's LUID."""
        factory = _factory()
        try:
            for a, d in _each_adapter(factory):
                try:
                    for o, desc in _outputs_of(a):
                        if desc.DeviceName.lower() == device_name.lower() \
                                and desc.Rotation == DXGI_MODE_ROTATION_IDENTITY:
                            return o, d.AdapterLuid.value
                        _release(o)
                finally:
                    _release(a)
        finally:
            _release(factory)
        raise OSError("no GPU output shows this monitor (or it is rotated)")

    def grab(self, wait_ms: int = 0) -> bool:
        """Takes the screen's latest frame into `texture`; False if none came (the texture, if any,
        is still the screen as it is: nothing was drawn since)."""
        info, res = _FRAMEINFO(), c_void_p()
        hr = _vcall(self._dup, _DUP_ACQUIRE, c_long, (c_uint, wait_ms), (POINTER(_FRAMEINFO), byref(info)),
                    (POINTER(c_void_p), byref(res)))
        if hr & 0xFFFFFFFF == DXGI_ERROR_WAIT_TIMEOUT:
            return False
        if hr < 0:
            raise OSError(f"screen capture failed (0x{hr & 0xFFFFFFFF:08X})")
        try:
            if info.LastPresentTime == 0 and self.texture:
                return False  # (only the mouse moved)
            frame = c_void_p()
            _call(res, _QI, (POINTER(GUID), byref(GUID.of(IID_ID3D11Texture2D))), (POINTER(c_void_p), byref(frame)))
            try:
                if not self.texture:
                    d = _texture_desc(frame)
                    mine = _TEXDESC(d.Width, d.Height, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0, 0, 0, 0, 0)
                    _call(self.devices.d3d, _D3D_CREATETEXTURE2D, (POINTER(_TEXDESC), byref(mine)), (c_void_p, None),
                          (POINTER(c_void_p), byref(self.texture)))
                    self.w, self.h = d.Width, d.Height
                _vcall(self.devices.context, _CTX_COPYRESOURCE, None, (c_void_p, self.texture), (c_void_p, frame))
            finally:
                _release(frame)
            self.present = info.LastPresentTime or qpc()
            return True
        finally:
            _release(res)
            _vcall(self._dup, _DUP_RELEASEFRAME, c_long)

    def keep_old(self) -> c_void_p:
        """The latest frame copied into the capture's one "old desktop" texture (made once, reused by
        every slide: a texture made per slide would stay allocated until the GPU's queue runs; one
        slide at a time per monitor). Owned by the capture."""
        if not self._old:
            mine = _TEXDESC(self.w, self.h, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0, 0, 0, 0, 0)
            _call(self.devices.d3d, _D3D_CREATETEXTURE2D, (POINTER(_TEXDESC), byref(mine)), (c_void_p, None),
                  (POINTER(c_void_p), byref(self._old)))
        _vcall(self.devices.context, _CTX_COPYRESOURCE, None, (c_void_p, self._old), (c_void_p, self.texture))
        return self._old

    def to_bitmap(self, texture: c_void_p) -> tuple[int, int, int]:
        """A texture's picture as a GDI memory bitmap (dc, bitmap, old object), read back through
        memory: slow, only for drawing on the CPU (a GPU failure in a slide, tests)."""
        st = c_void_p()
        desc = _TEXDESC(self.w, self.h, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0, D3D11_USAGE_STAGING, 0,
                        D3D11_CPU_ACCESS_READ, 0)
        _call(self.devices.d3d, _D3D_CREATETEXTURE2D, (POINTER(_TEXDESC), byref(desc)), (c_void_p, None),
              (POINTER(c_void_p), byref(st)))
        try:
            _vcall(self.devices.context, _CTX_COPYRESOURCE, None, (c_void_p, st), (c_void_p, texture))
            m = _MAPPED()
            _call(self.devices.context, _CTX_MAP, (c_void_p, st), (c_uint, 0), (c_uint, D3D11_MAP_READ), (c_uint, 0),
                  (POINTER(_MAPPED), byref(m)))
            try:
                bmp, bits = G.create_dib(self.w, self.h)
                if not bmp or not bits:
                    raise OSError("no bitmap")
                row = self.w * 4
                for y in range(self.h):
                    ctypes.memmove(bits + y * row, m.pData + y * m.RowPitch, row)
            finally:
                _vcall(self.devices.context, _CTX_UNMAP, None, (c_void_p, st), (c_uint, 0))
        finally:
            _release(st)
        dc = G.CreateCompatibleDC(0)
        return dc, bmp, G.SelectObject(dc, bmp)

    def use(self) -> None:
        """A slide starts using it: never closed under that slide (a display change, a setting)."""
        self.users += 1

    def done(self) -> None:
        """That slide ended: a capture dropped meanwhile is closed now."""
        self.users = max(0, self.users - 1)
        if self.retired and self.users == 0:
            self.close()

    def retire(self) -> None:
        """No longer handed out: closed now, or when the last slide using it ends."""
        self.retired = True
        if self.users == 0:
            self.close()

    def close(self) -> None:
        _release(self.texture)
        _release(self._old)
        _release(self._dup)
        self.texture, self._dup, self._old = c_void_p(), c_void_p(), c_void_p()
        owner, self.devices = self.devices, None
        if owner is not None and owner.layers > 0:
            owner.layers -= 1
            if owner.retired and owner.layers == 0:
                owner.close()


_captures: dict[str, Capture] = {}
_capture_failed: dict[str, float] = {}  # monitor -> time.monotonic() its capture last failed


WARM_AFTER_S = 1.0  # a lost capture is opened again this long after the last display change, when idle
FIRST_FRAME_MS = 50  # an opening waits at most this long for its first frame (none yet: it comes later)
_warm_wanted = False  # a capture was dropped or the displays changed: open them again (warm_due)
_displays_changed_at = 0.0


def capture_for(device_name: str, open_now: bool = True) -> Capture | None:
    """The GPU capture of a monitor (None: the GPU is off, or it can't be done: screenshots through
    memory then). `open_now` False (a slide): only one already open, never an opening (~100 ms+) in
    the key path; a missing one is opened later by warm_due. A failed opening is not tried again for
    RETRY_AFTER_S."""
    global _warm_wanted
    if not ENABLED or _D3D11CreateDevice is None:
        return None
    cap = _captures.get(device_name)
    if cap is not None:
        return cap
    if not open_now:
        _warm_wanted = True
        return None
    failed = _capture_failed.get(device_name)
    if failed is not None and time.monotonic() - failed < RETRY_AFTER_S:
        return None
    try:
        cap = Capture(device_name)
        cap.grab(FIRST_FRAME_MS)  # (no frame yet: its size is 0 until one comes; a slide checks the size)
    except (OSError, ValueError):
        if cap is not None:
            cap.close()
        _capture_failed[device_name] = time.monotonic()
        return None
    _capture_failed.pop(device_name, None)
    _captures[device_name] = cap
    return cap


def drop_capture(device_name: str, failed: bool = True) -> None:
    """A capture that failed (lost after a display change, the secure desktop): closed; with `failed`
    it is not tried again for RETRY_AFTER_S, else it is opened again when idle (warm_due)."""
    global _warm_wanted
    cap = _captures.pop(device_name, None)
    if cap is not None:
        cap.retire()  # (closed now, or by the slide still using it when it ends)
    if failed:
        _capture_failed[device_name] = time.monotonic()
    else:
        _warm_wanted = True


def drop_captures() -> None:
    """Every capture closed (the display setup or a GPU setting changed: opened again when needed)."""
    for name in list(_captures):
        drop_capture(name, failed=False)
    _capture_failed.clear()


def displays_changed() -> None:
    """The display setup changed (also a full-screen app changing the display mode, at each switch
    to or from it): which GPU drives each monitor is asked again. Nothing is opened now (it arrives
    as a slide starts: opening every monitor's capture held the slide still for most of a second).
    Captures that still work are kept; one Windows took away fails at its next use (that slide
    takes screenshots) and is opened again later by warm_due."""
    global _warm_wanted, _displays_changed_at
    _gpu_of.clear()
    _warm_wanted = True
    _displays_changed_at = time.monotonic()


def warm_due(idle: bool) -> None:
    """From the sync timer: opens ONE missing capture (one monitor per tick, ~100 ms each) when no
    slide runs (`idle`) and the displays have been quiet for WARM_AFTER_S; closes captures of
    monitors that are gone."""
    global _warm_wanted
    if not _warm_wanted or not idle or not ENABLED or time.monotonic() - _displays_changed_at < WARM_AFTER_S:
        return
    devices = [s.device for s in N.screens()]
    for name in [n for n in _captures if n not in devices]:
        drop_capture(name, failed=False)
    now = time.monotonic()
    missing = [d for d in devices if d not in _captures
               and now - _capture_failed.get(d, -RETRY_AFTER_S) >= RETRY_AFTER_S]
    if missing:
        capture_for(missing[0])
    _warm_wanted = len(missing) > 1


def warm_captures() -> None:
    """Opens every monitor's capture now (the first takes ~100 ms: never in a switch). After the GPU
    is set up and after a display change."""
    if not ENABLED:
        return
    for scr in N.screens():
        capture_for(scr.device)
