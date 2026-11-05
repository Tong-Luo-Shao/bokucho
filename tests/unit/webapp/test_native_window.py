from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

import bokucho.webapp.native_window as native_window

only_on_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Win32 ABI",
)


class _NativeHandle:
    def ToInt64(self) -> int:
        return 4242


class _InvokingNativeWindow:
    def __init__(self) -> None:
        self.Handle = _NativeHandle()
        self.InvokeRequired = True
        self.invocations = 0

    def Invoke(self, callback: Any) -> Any:
        self.invocations += 1
        return callback()


class _FakeUser32:
    def __init__(self) -> None:
        self.style = 0x16010000
        self.original_window_proc = 7777
        self.style_updates: list[tuple[int, int, int]] = []
        self.window_proc_updates: list[tuple[int, int, int]] = []
        self.forwarded_messages: list[tuple[int, int, int, int, int]] = []
        self.frame_updates: list[tuple[Any, ...]] = []
        self.window_rect = (-7, -7, 1927, 1087)
        self.monitor_rect = (0, 0, 1920, 1080)
        self.work_rect = (0, 0, 1920, 1032)
        self.cursor = (640, 16)
        self.zoomed = True
        self.sent_messages: list[tuple[int, int, int, int]] = []

    def GetWindowLongPtrW(self, handle: int, index: int) -> int:
        assert handle == 4242
        if index == native_window.GWL_STYLE:
            return self.style
        assert index == native_window.GWLP_WNDPROC
        return self.original_window_proc

    def SetWindowLongPtrW(self, handle: int, index: int, value: int) -> int:
        if index == native_window.GWL_STYLE:
            self.style_updates.append((handle, index, value))
            previous = self.style
            self.style = value
            return previous
        assert index == native_window.GWLP_WNDPROC
        self.window_proc_updates.append((handle, index, value))
        previous = self.original_window_proc
        self.original_window_proc = value
        return previous

    def CallWindowProcW(
        self,
        window_proc: int,
        handle: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        self.forwarded_messages.append((window_proc, handle, message, wparam, lparam))
        return 1

    def SetWindowPos(self, *arguments: Any) -> int:
        self.frame_updates.append(arguments)
        return 1

    def GetWindowRect(self, handle: int, rect: Any) -> int:
        assert handle == 4242
        self._set_rect(rect._obj, self.window_rect)
        return 1

    def MonitorFromWindow(self, handle: int, flags: int) -> int:
        assert handle == 4242
        assert flags == native_window.MONITOR_DEFAULTTONEAREST
        return 99

    def GetMonitorInfoW(self, monitor: int, info: Any) -> int:
        assert monitor == 99
        self._set_rect(info._obj.rcMonitor, self.monitor_rect)
        self._set_rect(info._obj.rcWork, self.work_rect)
        return 1

    def GetCursorPos(self, point: Any) -> int:
        point._obj.x, point._obj.y = self.cursor
        return 1

    def ReleaseCapture(self) -> int:
        return 1

    def SendMessageW(
        self,
        handle: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        self.sent_messages.append((handle, message, wparam, lparam))
        self.zoomed = False
        return 0

    def IsZoomed(self, handle: int) -> int:
        assert handle == 4242
        return int(self.zoomed)

    @staticmethod
    def _set_rect(target: Any, values: tuple[int, int, int, int]) -> None:
        target.left, target.top, target.right, target.bottom = values


class _FakeDwmApi:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int]] = []

    def DwmSetWindowAttribute(
        self,
        handle: int,
        attribute: int,
        value: Any,
        size: int,
    ) -> int:
        self.calls.append((handle, attribute, value._obj.value, size))
        return 0


@only_on_windows
def test_borderless_native_resize_installs_window_proc_and_sizing_style(
    monkeypatch,
) -> None:
    user32 = _FakeUser32()
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)
    monkeypatch.setattr(native_window, "_FRAME_SUBCLASSES", {})

    enabled = native_window.enable_borderless_resize(window)

    assert enabled is True
    assert len(user32.window_proc_updates) == 1
    assert user32.style_updates == [
        (
            4242,
            native_window.GWL_STYLE,
            0x16010000 | native_window.WS_THICKFRAME,
        )
    ]
    assert len(user32.frame_updates) == 1
    assert user32.frame_updates[0][0] == 4242
    assert user32.frame_updates[0][-1] & native_window.SWP_FRAMECHANGED

    window_proc = native_window._FRAME_SUBCLASSES[4242][0]
    assert window_proc(4242, native_window.WM_NCCALCSIZE, 1, 0) == 0


@only_on_windows
def test_native_window_opts_into_rounded_corners(monkeypatch) -> None:
    dwmapi = _FakeDwmApi()
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_DWMAPI", dwmapi, raising=False)

    assert native_window.enable_rounded_corners(window) is True
    assert dwmapi.calls == [
        (
            4242,
            native_window.DWMWA_WINDOW_CORNER_PREFERENCE,
            native_window.DWMWCP_ROUND,
            4,
        )
    ]


@pytest.mark.parametrize(
    ("edge", "hit_test"),
    [
        ("top", native_window.HTTOP),
        ("top-right", native_window.HTTOPRIGHT),
        ("right", native_window.HTRIGHT),
        ("bottom-right", native_window.HTBOTTOMRIGHT),
        ("bottom", native_window.HTBOTTOM),
        ("bottom-left", native_window.HTBOTTOMLEFT),
        ("left", native_window.HTLEFT),
        ("top-left", native_window.HTTOPLEFT),
    ],
)
def test_native_resize_gesture_uses_requested_edge(
    monkeypatch,
    edge: str,
    hit_test: int,
) -> None:
    user32 = _FakeUser32()
    user32.zoomed = False
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)

    assert native_window.begin_native_window_resize(window, edge) is True

    packed_cursor = (16 << 16) | 640
    assert user32.sent_messages == [
        (
            4242,
            native_window.WM_NCLBUTTONDOWN,
            hit_test,
            packed_cursor,
        )
    ]


def test_native_resize_gesture_is_disabled_while_maximized(monkeypatch) -> None:
    user32 = _FakeUser32()
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)

    assert native_window.begin_native_window_resize(window, "right") is False
    assert user32.sent_messages == []


@only_on_windows
def test_native_sizing_frame_is_removed_while_maximized(monkeypatch) -> None:
    user32 = _FakeUser32()
    user32.style |= native_window.WS_THICKFRAME
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)

    enabled = native_window.set_frameless_resize(window, False)

    assert enabled is True
    assert user32.style & native_window.WS_THICKFRAME == 0
    assert user32.frame_updates[0][-1] & native_window.SWP_FRAMECHANGED


def test_native_window_size_uses_exact_outer_bounds(monkeypatch) -> None:
    user32 = _FakeUser32()
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)

    sized = native_window.set_native_window_size(window, 1283, 765)

    assert sized is True
    assert user32.frame_updates == [
        (
            4242,
            0,
            0,
            0,
            1283,
            765,
            native_window.SWP_NOMOVE
            | native_window.SWP_NOZORDER
            | native_window.SWP_NOACTIVATE,
        )
    ]


def test_maximized_window_is_fitted_to_monitor_work_area(monkeypatch) -> None:
    user32 = _FakeUser32()
    window = SimpleNamespace(native=SimpleNamespace(Handle=_NativeHandle()))
    monkeypatch.setattr(native_window, "_USER32", user32)

    fitted = native_window.fit_maximized_to_work_area(window)

    assert fitted is True
    assert user32.frame_updates == [
        (
            4242,
            0,
            0,
            0,
            1920,
            1032,
            native_window.SWP_NOZORDER | native_window.SWP_NOACTIVATE,
        )
    ]


def test_native_caption_drag_restores_maximized_window(monkeypatch) -> None:
    user32 = _FakeUser32()
    native = _InvokingNativeWindow()
    window = SimpleNamespace(native=native)
    monkeypatch.setattr(native_window, "_USER32", user32)
    monkeypatch.setitem(
        sys.modules,
        "System",
        SimpleNamespace(Action=lambda callback: callback),
    )

    maximized = native_window.begin_native_window_drag(window)

    assert maximized is False
    assert native.invocations == 1
    packed_cursor = (16 << 16) | 640
    assert user32.sent_messages == [
        (
            4242,
            native_window.WM_NCLBUTTONDOWN,
            native_window.HTCAPTION,
            packed_cursor,
        )
    ]
