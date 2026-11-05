"""Windows integration for the frameless pywebview shell."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_THICKFRAME = 0x00040000

MONITOR_DEFAULTTONEAREST = 0x00000002

WM_NCLBUTTONDOWN = 0x00A1
WM_NCCALCSIZE = 0x0083
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

_RESIZE_HIT_TESTS = {
    "top": HTTOP,
    "top-right": HTTOPRIGHT,
    "right": HTRIGHT,
    "bottom-right": HTBOTTOMRIGHT,
    "bottom": HTBOTTOM,
    "bottom-left": HTBOTTOMLEFT,
    "left": HTLEFT,
    "top-left": HTTOPLEFT,
}

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
_E_INVALIDARG = 0x80070057

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

_FRAME_REFRESH_FLAGS = (
    SWP_NOSIZE
    | SWP_NOMOVE
    | SWP_NOZORDER
    | SWP_NOACTIVATE
    | SWP_FRAMECHANGED
)
_WINDOW_LONG_GETTER = (
    "GetWindowLongPtrW"
    if ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_longlong)
    else "GetWindowLongW"
)
_WINDOW_LONG_SETTER = (
    "SetWindowLongPtrW"
    if ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_longlong)
    else "SetWindowLongW"
)
_WindowProcedure = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
_FRAME_SUBCLASSES: dict[int, tuple[object, int]] = {}


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _load_user32() -> object | None:
    if sys.platform != "win32":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = getattr(user32, _WINDOW_LONG_GETTER)
    get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_window_long.restype = ctypes.c_ssize_t
    set_window_long = getattr(user32, _WINDOW_LONG_SETTER)
    set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_window_long.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.RECT),
    ]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(_MonitorInfo),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    return user32


def _load_dwmapi() -> object | None:
    if sys.platform != "win32":
        return None

    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    return dwmapi


_USER32 = _load_user32()
_DWMAPI = _load_dwmapi()


def _native_handle(window: object) -> int:
    native = getattr(window, "native", None)
    if native is None:
        raise RuntimeError("The native window is not initialized")

    handle = native.Handle
    to_int64 = getattr(handle, "ToInt64", None)
    return int(to_int64() if to_int64 is not None else handle)


def _raise_last_windows_error() -> None:
    error_code = ctypes.get_last_error()
    if error_code:
        raise ctypes.WinError(error_code)


def enable_borderless_resize(window: object) -> bool:
    """Enable native edge resizing while extending the client over its frame."""
    if not _install_borderless_window_proc(window):
        return False
    return set_frameless_resize(window, True)


def enable_rounded_corners(window: object) -> bool:
    """Opt a frameless window into Windows 11's standard corner radius."""
    if _DWMAPI is None:
        return False

    preference = wintypes.DWORD(DWMWCP_ROUND)
    result = int(
        _DWMAPI.DwmSetWindowAttribute(
            _native_handle(window),
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    )
    if result == 0:
        return True
    if result & 0xFFFFFFFF == _E_INVALIDARG:
        return False
    raise OSError(
        f"DwmSetWindowAttribute failed with HRESULT 0x{result & 0xFFFFFFFF:08X}"
    )


def set_frameless_resize(window: object, enabled: bool) -> bool:
    """Toggle the native sizing frame around the frameless client area."""
    if _USER32 is None:
        return False

    handle = _native_handle(window)
    get_window_long = getattr(_USER32, _WINDOW_LONG_GETTER)
    set_window_long = getattr(_USER32, _WINDOW_LONG_SETTER)
    style = int(get_window_long(handle, GWL_STYLE))
    updated_style = (
        style | WS_THICKFRAME
        if enabled
        else style & ~WS_THICKFRAME
    )
    if updated_style != style:
        ctypes.set_last_error(0)
        previous_style = int(set_window_long(handle, GWL_STYLE, updated_style))
        if previous_style == 0:
            _raise_last_windows_error()

    refreshed = _USER32.SetWindowPos(
        handle,
        0,
        0,
        0,
        0,
        0,
        _FRAME_REFRESH_FLAGS,
    )
    if not refreshed:
        _raise_last_windows_error()
    return True


def set_native_window_size(window: object, width: int, height: int) -> bool:
    """Set the exact outer bounds of the frameless native window."""
    if _USER32 is None:
        return False

    sized = _USER32.SetWindowPos(
        _native_handle(window),
        0,
        0,
        0,
        width,
        height,
        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
    )
    if not sized:
        _raise_last_windows_error()
        return False
    return True


def _install_borderless_window_proc(window: object) -> bool:
    if _USER32 is None:
        return False

    handle = _native_handle(window)
    if handle in _FRAME_SUBCLASSES:
        return True

    previous_window_proc = 0

    def window_proc(
        target: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if message == WM_NCCALCSIZE and wparam:
            return 0
        return int(
            _USER32.CallWindowProcW(
                previous_window_proc,
                target,
                message,
                wparam,
                lparam,
            )
        )

    callback = _WindowProcedure(window_proc)
    callback_address = int(ctypes.cast(callback, ctypes.c_void_p).value or 0)
    set_window_long = getattr(_USER32, _WINDOW_LONG_SETTER)
    ctypes.set_last_error(0)
    previous_window_proc = int(
        set_window_long(handle, GWLP_WNDPROC, callback_address)
    )
    if previous_window_proc == 0:
        _raise_last_windows_error()
    _FRAME_SUBCLASSES[handle] = (callback, previous_window_proc)
    return True


def fit_maximized_to_work_area(window: object) -> bool:
    """Constrain a maximized frameless window to the monitor's usable area."""
    if _USER32 is None:
        return False

    handle = _native_handle(window)
    monitor = _USER32.MonitorFromWindow(handle, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        _raise_last_windows_error()
        return False

    monitor_info = _MonitorInfo()
    monitor_info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not _USER32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
        _raise_last_windows_error()
        return False

    window_rect = wintypes.RECT()
    if not _USER32.GetWindowRect(handle, ctypes.byref(window_rect)):
        _raise_last_windows_error()
        return False

    work = monitor_info.rcWork
    current_bounds = (
        window_rect.left,
        window_rect.top,
        window_rect.right,
        window_rect.bottom,
    )
    work_bounds = (work.left, work.top, work.right, work.bottom)
    if current_bounds == work_bounds:
        return True

    fitted = _USER32.SetWindowPos(
        handle,
        0,
        work.left,
        work.top,
        work.right - work.left,
        work.bottom - work.top,
        SWP_NOZORDER | SWP_NOACTIVATE,
    )
    if not fitted:
        _raise_last_windows_error()
        return False
    return True


def begin_native_window_resize(window: object, edge: str) -> bool:
    """Transfer the current left-button gesture to Windows edge resizing."""
    try:
        hit_test = _RESIZE_HIT_TESTS[edge]
    except KeyError as error:
        raise ValueError(f"Unsupported resize edge: {edge}") from error

    if _USER32 is None:
        return False

    handle = _native_handle(window)
    if _USER32.IsZoomed(handle):
        return False
    native = window.native

    def begin_resize() -> None:
        cursor = wintypes.POINT()
        if not _USER32.GetCursorPos(ctypes.byref(cursor)):
            _raise_last_windows_error()
            return

        packed_cursor = ((cursor.y & 0xFFFF) << 16) | (cursor.x & 0xFFFF)
        _USER32.ReleaseCapture()
        _USER32.SendMessageW(handle, WM_NCLBUTTONDOWN, hit_test, packed_cursor)

    if getattr(native, "InvokeRequired", False):
        from System import Action

        native.Invoke(Action(begin_resize))
    else:
        begin_resize()
    return True


def begin_native_window_drag(window: object) -> bool:
    """Transfer the current left-button gesture to Windows caption dragging."""
    if _USER32 is None:
        return False

    handle = _native_handle(window)
    native = window.native

    def begin_drag() -> None:
        cursor = wintypes.POINT()
        if not _USER32.GetCursorPos(ctypes.byref(cursor)):
            _raise_last_windows_error()
            return

        packed_cursor = ((cursor.y & 0xFFFF) << 16) | (cursor.x & 0xFFFF)
        _USER32.ReleaseCapture()
        _USER32.SendMessageW(handle, WM_NCLBUTTONDOWN, HTCAPTION, packed_cursor)

    if getattr(native, "InvokeRequired", False):
        from System import Action

        native.Invoke(Action(begin_drag))
    else:
        begin_drag()
    return bool(_USER32.IsZoomed(handle))
