"""The patcher's window: the game folder, apply, restore, close."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk

from bokucho.patcher.core import (
    PatchError,
    State,
    applied_message,
    apply,
    default_folder,
    describe,
    inspect,
    restore,
    restored_message,
)
from bokucho.patcher.payload import Patch

SOURCE_URL = "https://github.com/Tong-Luo-Shao/bokucho"


def show_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("補丁", message)
    root.destroy()


def run(patch: Patch, start: Path) -> None:
    _PatcherWindow(patch, start).root.mainloop()


class _PatcherWindow:
    def __init__(self, patch: Patch, start: Path) -> None:
        self.patch = patch
        self.start = start
        self.root = tk.Tk()
        self.root.title(patch.title)
        self.root.resizable(False, False)
        self.folder = tk.StringVar(value=str(default_folder(start, patch) or ""))
        self.status = tk.StringVar()
        frame = ttk.Frame(self.root, padding=16)
        frame.grid()
        self._folder_row(frame)
        self._log_rows(frame)
        self._button_row(frame)
        ttk.Label(
            frame,
            text=f"以 Bokucho 製作（GPL-3.0-or-later）。原始碼：{SOURCE_URL}",
            foreground="gray",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.root.after(0, self._initialize)

    def _folder_row(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="遊戲資料夾").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=self.folder, width=56)
        entry.grid(row=1, column=0, sticky="we")
        entry.bind("<Return>", lambda _event: self._refresh())
        self.browse = ttk.Button(frame, text="瀏覽…", command=self._choose)
        self.browse.grid(row=1, column=1, padx=(8, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=480, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 8)
        )

    def _log_rows(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="日誌").grid(row=3, column=0, columnspan=2, sticky="w")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=4, column=0, columnspan=2, sticky="we", pady=(4, 8))
        log_frame.columnconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=7, width=64, wrap="word", state="disabled")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=log_scroll.set)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=5, column=0, columnspan=2, sticky="we")

    def _button_row(self, frame: ttk.Frame) -> None:
        patch = self.patch
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.apply_button = ttk.Button(
            buttons,
            text="套用",
            command=lambda: self._work(
                "套用",
                lambda path, log: (apply(path, patch, log), applied_message(patch, path))[1],
            ),
        )
        self.restore_button = ttk.Button(
            buttons,
            text="還原",
            command=lambda: self._work(
                "還原", lambda path, log: restored_message(restore(path, patch, log))
            ),
        )
        self.close_button = ttk.Button(buttons, text="關閉", command=self.root.destroy)
        for column, button in enumerate(
            (self.apply_button, self.restore_button, self.close_button)
        ):
            button.grid(row=0, column=column, padx=(8, 0))

    def _write_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message.rstrip() + "\n")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def _refresh(self) -> None:
        chosen = Path(self.folder.get()) if self.folder.get() else None
        if chosen is None or not chosen.is_dir():
            self.status.set("請選擇遊戲所在的資料夾。")
            self.apply_button.state(["disabled"])
            self.restore_button.state(["disabled"])
            return
        found = inspect(chosen, self.patch)
        self.status.set(describe(found, self.patch))
        self.apply_button.state(["!disabled"] if found.state is State.ORIGINAL else ["disabled"])
        self.restore_button.state(["!disabled"] if found.restorable else ["disabled"])

    def _work(self, name: str, action: Callable[[Path, Callable[[str], None]], str]) -> None:
        chosen = Path(self.folder.get())
        for control in (self.browse, self.apply_button, self.restore_button, self.close_button):
            control.state(["disabled"])
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.progress.start(12)
        self.status.set(
            "編譯並套用中，請稍候…" if self.patch.recipe is not None else "處理中，請稍候…"
        )
        outcome: list[tuple[bool, str]] = []
        pending: SimpleQueue[str] = SimpleQueue()
        self._write_log(f"開始{name}：{chosen}")

        def background() -> None:
            try:
                outcome.append((True, action(chosen, pending.put)))
            except PatchError as error:
                outcome.append((False, str(error)))
            except Exception as error:  # noqa: BLE001 - shown to the player, not swallowed
                outcome.append((False, f"發生未預期的錯誤：{error}"))

        thread = threading.Thread(target=background, daemon=True)
        thread.start()
        self._finish(name, thread, pending, outcome)

    def _finish(
        self,
        name: str,
        thread: threading.Thread,
        pending: SimpleQueue[str],
        outcome: list[tuple[bool, str]],
    ) -> None:
        while True:
            try:
                self._write_log(pending.get_nowait())
            except Empty:
                break
        if thread.is_alive():
            self.root.after(100, self._finish, name, thread, pending, outcome)
            return
        self.progress.stop()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.browse.state(["!disabled"])
        self.close_button.state(["!disabled"])
        self._refresh()
        succeeded, message = outcome[0]
        if succeeded:
            self._write_log(f"{name}完成：{message}")
            messagebox.showinfo(self.patch.title, message)
        else:
            self._write_log(f"{name}失敗：{message}")
            messagebox.showerror(self.patch.title, message)

    def _choose(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder.get() or str(self.start))
        if chosen:
            self.folder.set(chosen)
            self._refresh()
            self._write_log(f"遊戲資料夾：{chosen}")

    def _initialize(self) -> None:
        self._refresh()
        if self.folder.get():
            self._write_log(f"遊戲資料夾：{self.folder.get()}")
        else:
            self._write_log("等待選擇遊戲資料夾。")
