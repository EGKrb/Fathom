# Fathom addition to dumpulator: the windows of the emulated program (user32 and the text of gdi32),
# with no screen. Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# Message boxes are answered with their default button and recorded. Window classes and windows
# exist in Python: CreateWindowEx sends WM_NCCREATE and WM_CREATE to the window procedure,
# DestroyWindow WM_DESTROY and WM_NCDESTROY, each thread has a message queue (GetMessage,
# PeekMessage, DispatchMessage, PostMessage, SendMessage, PostQuitMessage), timers follow the virtual
# clock (GetMessage waits for the next one with Sleep). A simulated user: when a thread's queue is
# empty and no timer is pending, it closes the thread's windows (WM_CLOSE), as one closes an
# application; dialog boxes get WM_INITDIALOG then a click on OK. The text the program draws
# (DrawText, TextOut, ExtTextOut) and sets as window titles is recorded. What the windows show is
# drawn into images (gui_draw.py): ShowWindow / UpdateWindow / InvalidateRect bring WM_PAINT, and
# the functions of gdi32 draw on the window's canvas; PNG images are saved (--screenshots).
#
# The windows of the processes of a run are one set (FindWindow, FindWindowEx with message-only
# windows, GetWindowThreadProcessId, RegisterWindowMessage): a message posted to a window or a thread
# of another process goes to that thread's queue, a message sent to it (SendMessage) waits until the
# receiving thread handles it in its GetMessage / PeekMessage, as natively; WM_COPYDATA, WM_SETTEXT and
# WM_GETTEXT carry their data between the processes. A GetMessage with nothing to do waits for the
# other processes before the simulated user closes the windows.

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .apihooks import Invoke, read_string, read_wstring, to_signed
from .gui_draw import Drawing, GdiObject, SYSTEM_COLORS, STOCK_BASE

WM_CREATE, WM_DESTROY, WM_CLOSE, WM_QUIT, WM_NCCREATE, WM_NCDESTROY = 0x1, 0x2, 0x10, 0x12, 0x81, 0x82
WM_TIMER, WM_INITDIALOG, WM_COMMAND, WM_PAINT = 0x113, 0x110, 0x111, 0xF
WM_ERASEBKGND = 0x14
IDOK, IDCANCEL, IDABORT, IDRETRY, IDYES, IDTRYAGAIN = 1, 2, 3, 4, 6, 10

PREDEFINED_CLASSES = {"button", "edit", "static", "listbox", "combobox", "scrollbar", "richedit20w", "#32770",
                      "msctls_progress32", "syslistview32", "systreeview32", "systabcontrol32"}


@dataclass
class WindowClass:
    name: str
    wndproc: int
    wide: bool


@dataclass
class Window:
    hwnd: int
    class_name: str
    wndproc: int
    title: str
    style: int
    exstyle: int
    parent: int
    thread: int
    wide: bool
    menu: int = 0
    rect: tuple = (0, 0, 640, 480)
    longs: Dict[int, int] = field(default_factory=dict)
    alive: bool = True
    dialog: bool = False
    dialog_result: Optional[int] = None
    gui: object = field(default=None, repr=False, compare=False)   # the Gui of its process


@dataclass
class Timer:
    due: int        # virtual time (100 ns)
    period: int     # 100 ns
    hwnd: int
    id: int
    proc: int
    thread: int


class Gui:
    def __init__(self, dp):
        self.dp = dp
        self.classes: Dict[str, WindowClass] = {}
        self._windows: Dict[int, Window] = {}
        self.queues: Dict[int, deque] = {}
        self.sent: Dict[int, deque] = {}            # thread -> messages sent by other processes
        self.queue_events: Dict[int, tuple] = {}    # thread -> (EventObject, handle): a message came
        self.idle_waited: Dict[int, bool] = {}
        self.quit: Dict[int, int] = {}
        self.timers: List[Timer] = []
        self.closed: set = set()          # threads whose windows the simulated user closed
        self.idle_peeks: Dict[int, int] = {}
        self.next_handle = 0x10020
        self.next_atom = 0xC100
        self.next_timer = 0x7F00
        self.events: List[dict] = []
        self.drawing = Drawing(self)

    # ------------------------------------------------------------------ helpers
    @property
    def scheduler(self):
        return self.dp.scheduler

    def _now(self) -> int:
        return self.scheduler.interrupt_time

    @property
    def world(self):
        return getattr(self.dp, "world", None)

    @property
    def windows(self) -> Dict[int, Window]:
        """The windows of every process of the run."""
        world = self.world
        if world is None:
            return self._windows
        if getattr(world, "windows", None) is None:
            world.windows = {}
        if self._windows:
            world.windows.update(self._windows)
            self._windows = {}
        return world.windows

    def _handle(self) -> int:
        world = self.world
        if world is not None:
            world.next_gui_handle = getattr(world, "next_gui_handle", 0x10020) + 4
            return world.next_gui_handle
        self.next_handle += 4
        return self.next_handle

    def _others_alive(self) -> bool:
        world = self.world
        return world is not None and any(p is not self.dp for p in world.live())

    def _queue_event(self, thread: int):
        """(EventObject, handle in this process) signaled when a message comes for the thread."""
        if thread not in self.queue_events:
            from .handles import EventObject
            from .ntenums import EVENT_TYPE
            event = EventObject(EVENT_TYPE.SynchronizationEvent, False)
            self.queue_events[thread] = (event, self.dp.handles.new(event))
        return self.queue_events[thread]

    def _wake(self, thread: int):
        event, _ = self._queue_event(thread)
        event.signalled = True
        world = self.world
        if world is not None:
            world.signal_all()
        else:
            self.scheduler.signal()

    def _post(self, thread: int, message: tuple):
        self._queue(thread).append(message)
        self._wake(thread)

    def _gui_of_thread(self, thread: int):
        world = self.world
        for process in (world.live() if world is not None else [self.dp]):
            if thread in process.scheduler.threads:
                return getattr(process, "gui", None)
        return None

    def _record(self, kind: str, **values):
        values["kind"] = kind
        values["thread"] = self.dp.thread_id
        self.events.append(values)
        self.dp.info(f"[gui] {kind}: " + ", ".join(f"{k}={v!r}" for k, v in values.items() if k not in ("kind", "thread")))

    def _class_name(self, value: int, wide: bool) -> str:
        if value < 0x10000:
            return f"#{value}"
        return read_string(self.dp, value, wide) or ""

    def _queue(self, thread: int) -> deque:
        return self.queues.setdefault(thread, deque())

    def _write_msg(self, address: int, hwnd: int, message: int, wparam: int, lparam: int):
        dp = self.dp
        time_ms = (self._now() // 10_000) & 0xFFFFFFFF
        if dp._x64:
            data = struct.pack("<QIIQQIiiI", hwnd, message, 0, wparam, lparam, time_ms, 100, 100, 0)
        else:
            data = struct.pack("<IIIIIii", hwnd, message, wparam & 0xFFFFFFFF, lparam & 0xFFFFFFFF, time_ms, 100, 100)
        dp.write(address, data)

    def _read_msg(self, address: int):
        dp = self.dp
        if dp._x64:
            hwnd, message, _, wparam, lparam = struct.unpack("<QIIQQ", bytes(dp.read(address, 32)))
        else:
            hwnd, message, wparam, lparam = struct.unpack("<IIII", bytes(dp.read(address, 16)))
        return hwnd, message, wparam, lparam

    def _wndproc_call(self, window: Window, message: int, wparam: int, lparam: int, then=None) -> Invoke:
        return Invoke(window.wndproc, [window.hwnd, message, wparam, lparam], then)

    # ------------------------------------------------------------------ message boxes
    @staticmethod
    def _default_answer(style: int) -> int:
        buttons = style & 0xF
        default = (style >> 8) & 0xF  # MB_DEFBUTTON2 = 0x100...
        choices = {0: [IDOK], 1: [IDOK, IDCANCEL], 2: [IDABORT, IDRETRY, 5], 3: [IDYES, 7, IDCANCEL],
                   4: [IDYES, 7], 5: [IDRETRY, IDCANCEL], 6: [IDCANCEL, IDTRYAGAIN, 11]}.get(buttons, [IDOK])
        return choices[min(default, len(choices) - 1)]

    def message_box(self, dp, hwnd, text, caption, style, language, timeout, wide):
        text_s, caption_s = read_string(dp, text, wide), read_string(dp, caption, wide)
        answer = self._default_answer(style)
        self._record("message_box", caption=caption_s, text=text_s, style=hex(style), answer=answer)
        labels = {0: ["OK"], 1: ["OK", "Cancel"], 2: ["Abort", "Retry", "Ignore"], 3: ["Yes", "No", "Cancel"],
                  4: ["Yes", "No"], 5: ["Retry", "Cancel"], 6: ["Cancel", "Try Again", "Continue"]}.get(style & 0xF, ["OK"])
        self.drawing.save(self.drawing.message_box(caption_s, text_s, labels), "message box " + (caption_s or ""))
        return answer

    def message_box_indirect(self, dp, params, wide):
        ptr = dp.ptr_size()
        # MSGBOXPARAMS: cbSize, hwndOwner, hInstance, lpszText, lpszCaption, dwStyle...
        text = dp.read_ptr(params + (3 * ptr if dp._x64 else 12))
        caption = dp.read_ptr(params + (4 * ptr if dp._x64 else 16))
        style = dp.read_ulong(params + (5 * ptr if dp._x64 else 20))
        return self.message_box(dp, 0, text, caption, style, 0, 0, wide)

    # ------------------------------------------------------------------ classes and windows
    def register_class_ex(self, dp, wndclass, wide):
        if dp._x64:
            wndproc, class_ptr = dp.read_ptr(wndclass + 8), dp.read_ptr(wndclass + 64)
        else:
            wndproc, class_ptr = dp.read_ptr(wndclass + 8), dp.read_ptr(wndclass + 40)
        return self._register(self._class_name(class_ptr, wide), wndproc, wide)

    def register_class(self, dp, wndclass, wide):
        if dp._x64:
            wndproc, class_ptr = dp.read_ptr(wndclass + 8), dp.read_ptr(wndclass + 64)
        else:
            wndproc, class_ptr = dp.read_ptr(wndclass + 4), dp.read_ptr(wndclass + 36)
        return self._register(self._class_name(class_ptr, wide), wndproc, wide)

    def _register(self, name: str, wndproc: int, wide: bool) -> int:
        self.classes[name.lower()] = WindowClass(name, wndproc, wide)
        self.next_atom += 1
        self.classes[f"#{self.next_atom}"] = self.classes[name.lower()]
        self._record("register_class", name=name)
        return self.next_atom

    def create_window(self, dp, exstyle, class_ptr, title_ptr, style, x, y, width, height, parent, menu, instance, param, wide):
        class_name = self._class_name(class_ptr, wide)
        title = read_string(dp, title_ptr, wide) if title_ptr else ""
        cls = self.classes.get(class_name.lower())
        if cls is None and class_name.lower() not in PREDEFINED_CLASSES:
            from .apihooks import set_last_error
            set_last_error(dp, 1407)  # ERROR_CANNOT_FIND_WND_CLASS
            self._record("create_window_failed", class_name=class_name, title=title)
            return 0
        hwnd = self._handle()
        x, y, width, height = (to_signed(v) for v in (x, y, width, height))
        if width < 0:
            width, height = 640, 480
        window = Window(hwnd, class_name, cls.wndproc if cls else 0, title, style & 0xFFFFFFFF, exstyle & 0xFFFFFFFF,
                        parent, dp.thread_id, cls.wide if cls else wide, menu, (max(x, 0), max(y, 0), max(x, 0) + width, max(y, 0) + height))
        window.gui = self
        self.windows[hwnd] = window
        self._record("create_window", hwnd=hex(hwnd), class_name=class_name, title=title)
        if not window.wndproc:
            return hwnd
        # CREATESTRUCT for WM_NCCREATE / WM_CREATE
        if dp._x64:
            cs = struct.pack("<QQQQiiiiIIQQI", param, instance, menu, parent, height, width, y, x, style & 0xFFFFFFFF, 0,
                             title_ptr, class_ptr, exstyle & 0xFFFFFFFF) + bytes(4)
        else:
            cs = struct.pack("<IIIIiiiiIIII", param, instance, menu, parent, height, width, y, x, style & 0xFFFFFFFF,
                             title_ptr, class_ptr, exstyle & 0xFFFFFFFF)
        cs_ptr = dp.allocate(len(cs))
        dp.write(cs_ptr, cs)

        def after_create(result):
            if to_signed(result) == -1:
                window.alive = False
                return 0
            return hwnd

        def after_nccreate(result):
            if result == 0:
                window.alive = False
                return 0
            return self._wndproc_call(window, WM_CREATE, 0, cs_ptr, after_create)
        return self._wndproc_call(window, WM_NCCREATE, 0, cs_ptr, after_nccreate)

    def destroy_window(self, dp, hwnd):
        window = self.windows.get(hwnd)
        if window is None or not window.alive:
            return 0
        if window.gui is not None and window.gui is not self:
            from .apihooks import set_last_error
            set_last_error(dp, 5)  # ERROR_ACCESS_DENIED: a window of another process
            return 0
        self.drawing.save_window(window)
        window.alive = False
        self.timers = [t for t in self.timers if t.hwnd != hwnd]
        self._record("destroy_window", hwnd=hex(hwnd), title=window.title)
        if not window.wndproc:
            return 1
        return self._wndproc_call(window, WM_DESTROY, 0, 0,
                                  lambda _: self._wndproc_call(window, WM_NCDESTROY, 0, 0, lambda _: 1))

    def def_window_proc(self, dp, hwnd, message, wparam, lparam):
        if message == WM_CLOSE:
            return self.destroy_window(dp, hwnd)
        if message == WM_NCCREATE:
            return 1
        if message == WM_ERASEBKGND:
            return 1
        return 0

    def invalidate(self, dp, hwnd, *rest):
        """InvalidateRect: a WM_PAINT in the window's queue (once)."""
        window = self.windows.get(hwnd)
        if window is not None and window.alive and window.wndproc:
            queue = self._queue(window.thread)
            if not any(m[0] == hwnd and m[1] == WM_PAINT for m in queue):
                queue.append((hwnd, WM_PAINT, 0, 0))
        return 1

    def update_window(self, dp, hwnd):
        """UpdateWindow: WM_PAINT now, if the window waits for one."""
        window = self.windows.get(hwnd)
        if window is None or not window.alive or not window.wndproc:
            return 1
        queue = self._queue(window.thread)
        pending = [m for m in queue if m[0] == hwnd and m[1] == WM_PAINT]
        if not pending:
            return 1
        for m in pending:
            queue.remove(m)
        return self._wndproc_call(window, WM_PAINT, 0, 0, lambda _: 1)

    def show_window(self, dp, hwnd, command):
        window = self.windows.get(hwnd)
        if window is not None:
            self._record("show_window", hwnd=hex(hwnd), command=command)
            if command:
                self.invalidate(dp, hwnd)
        return 0

    def window_long(self, dp, hwnd, index, value=None):
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        index = to_signed(index)
        current = {-4: window.wndproc, -16: window.style, -20: window.exstyle, -12: window.menu}.get(index, window.longs.get(index, 0))
        if value is not None:
            if index == -4:
                window.wndproc = value
            elif index == -16:
                window.style = value
            elif index == -20:
                window.exstyle = value
            else:
                window.longs[index] = value
        return current

    def get_rect(self, dp, hwnd, rect, client):
        window = self.windows.get(hwnd)
        left, top, right, bottom = window.rect if window else (0, 0, 1920, 1080)
        if client:
            right, bottom, left, top = right - left, bottom - top, 0, 0
        dp.write(rect, struct.pack("<iiii", left, top, right, bottom))
        return 1

    def set_window_text(self, dp, hwnd, text, wide):
        window = self.windows.get(hwnd)
        value = read_string(dp, text, wide) or ""
        if window is not None:
            window.title = value
        self._record("set_window_text", hwnd=hex(hwnd), text=value)
        return 1

    def get_window_text(self, dp, hwnd, buffer, size, wide):
        window = self.windows.get(hwnd)
        text = (window.title if window else "")[:max(0, size - 1)]
        data = text.encode("utf-16-le") + b"\0\0" if wide else text.encode("mbcs", "replace") + b"\0"
        if size > 0:
            dp.write(buffer, data)
        return len(text)

    def find_window(self, dp, class_ptr, title_ptr, wide, parent=0, after=0, extended=False):
        """FindWindow: the top-level windows of every process; FindWindowEx: the children of parent
        (0: the top-level windows, HWND_MESSAGE: the message-only windows), after `after`."""
        class_name = self._class_name(class_ptr, wide).lower() if class_ptr else None
        title = read_string(dp, title_ptr, wide) if title_ptr else None
        message_only = (parent & 0xFFFFFFFF) == 0xFFFFFFFD
        started = not after
        for window in sorted(self.windows.values(), key=lambda w: w.hwnd):
            if not started:
                started = window.hwnd == after
                continue
            is_message = (window.parent & 0xFFFFFFFF) == 0xFFFFFFFD
            if message_only:
                if not is_message:
                    continue
            elif extended and parent:
                if window.parent != parent:
                    continue
            elif window.parent and not is_message or (is_message and not extended):
                continue
            elif is_message:
                continue
            if window.alive and (class_name is None or window.class_name.lower() == class_name) and (title is None or window.title == title):
                return window.hwnd
        return 0

    def window_thread_process(self, dp, hwnd, pid_ptr):
        window = self.windows.get(hwnd)
        if window is None:
            return 0
        if pid_ptr:
            owner = window.gui.dp if window.gui is not None else dp
            dp.write_ulong(pid_ptr, owner.process_id)
        return window.thread

    def register_window_message(self, dp, name_ptr, wide):
        name = (read_string(dp, name_ptr, wide) or "").lower()
        world = self.world
        table = getattr(world, "window_messages", None) if world is not None else getattr(self, "_window_messages", None)
        if table is None:
            table = {}
            if world is not None:
                world.window_messages = table
            else:
                self._window_messages = table
        if name not in table:
            table[name] = 0xC000 + len(table) + 0x100
        return table[name]

    # ------------------------------------------------------------------ messages
    def post_message(self, dp, hwnd, message, wparam, lparam):
        if (hwnd & 0xFFFFFFFF) == 0xFFFF:  # HWND_BROADCAST: every top-level window of the run
            for window in list(self.windows.values()):
                if window.alive and not window.parent:
                    (window.gui or self)._post(window.thread, (window.hwnd, message, wparam, lparam))
            return 1
        window = self.windows.get(hwnd)
        if hwnd and (window is None or not window.alive):
            from .apihooks import set_last_error
            set_last_error(dp, 1400)  # ERROR_INVALID_WINDOW_HANDLE
            return 0
        owner = window.gui if window is not None and window.gui is not None else self
        thread = window.thread if window else dp.thread_id
        if owner is not self:
            self._record("post_message_to_process", hwnd=hex(hwnd), message=hex(message), pid=owner.dp.process_id)
        owner._post(thread, (hwnd, message, wparam, lparam))
        return 1

    def post_thread_message(self, dp, thread, message, wparam, lparam):
        owner = self._gui_of_thread(thread) or self
        owner._post(thread, (0, message, wparam, lparam))
        return 1

    def send_message(self, dp, hwnd, message, wparam, lparam, wide=True):
        if (hwnd & 0xFFFFFFFF) == 0xFFFF:  # HWND_BROADCAST: posted to the others (a send would wait for all)
            self.post_message(dp, hwnd, message, wparam, lparam)
            return 0
        window = self.windows.get(hwnd)
        if window is None or not window.alive:
            return 0
        if window.gui is not None and window.gui is not self:
            return self._send_to_process(dp, window, message, wparam, lparam, wide)
        if not window.wndproc:
            return self.def_window_proc(dp, hwnd, message, wparam, lparam)
        return self._wndproc_call(window, message, wparam, lparam)

    def send_notify(self, dp, hwnd, message, wparam, lparam):
        window = self.windows.get(hwnd)
        if window is not None and window.gui is not None and window.gui is not self:
            return self.post_message(dp, hwnd, message, wparam, lparam)
        return self.send_message(dp, hwnd, message, wparam, lparam)

    def send_message_timeout(self, dp, hwnd, message, wparam, lparam, flags, timeout, result_ptr, wide):
        def done(value):
            if result_ptr:
                dp.write_ptr(result_ptr, value & dp.addr_mask)
            return 1
        result = self.send_message(dp, hwnd, message, wparam, lparam, wide)
        if isinstance(result, Invoke):
            original = result.then

            def then(value):
                out = original(value) if original is not None else value
                return done(out) if not isinstance(out, Invoke) else out
            result.then = then
            return result
        return done(result)

    # ------------------------------------------------------------------ messages between processes
    def _marshal(self, receiver, window, message, wparam, lparam, wide):
        """The lparam of a message for another process (its data copied there), and a function that
        copies an answer back (WM_GETTEXT)."""
        dp, rdp = self.dp, receiver.dp
        if message == 0x4A and lparam:  # WM_COPYDATA: COPYDATASTRUCT {dwData, cbData, lpData}
            if dp._x64:
                data_value, size, pointer = struct.unpack("<QI4xQ", bytes(dp.read(lparam, 24)))
            else:
                data_value, size, pointer = struct.unpack("<III", bytes(dp.read(lparam, 12)))
            data = bytes(dp.read(pointer, size)) if size and pointer else b""
            copy = rdp.allocate(max(1, len(data)))
            if data:
                rdp.write(copy, data)
            if rdp._x64:
                block = struct.pack("<QI4xQ", data_value, len(data), copy if data else 0)
            else:
                block = struct.pack("<III", data_value & 0xFFFFFFFF, len(data), copy if data else 0)
            target = rdp.allocate(len(block))
            rdp.write(target, block)
            self._record("copy_data", pid=rdp.process_id, size=len(data), data=data[:256].hex())
            return target, None
        if message == 0xC and lparam:  # WM_SETTEXT
            text = read_string(dp, lparam, wide) or ""
            from .apihooks import write_string
            return write_string(rdp, text, window.wide), None
        if message == 0xD and lparam and wparam:  # WM_GETTEXT: a buffer there, copied back here
            size = (wparam & 0xFFFF) * (2 if window.wide else 1)
            buffer = rdp.allocate(size + 2)

            def back():
                text = read_string(rdp, buffer, window.wide) or ""
                text = text[:max(0, (wparam & 0xFFFF) - 1)]
                data = text.encode("utf-16-le") + b"\0\0" if wide else text.encode("mbcs", "replace") + b"\0"
                dp.write(lparam, data)
            return buffer, back
        return lparam, None

    def _send_to_process(self, dp, window, message, wparam, lparam, wide):
        """SendMessage to a window of another process: the sender waits until the receiving thread
        has handled it (in its GetMessage / PeekMessage), then gets its result."""
        from .handles import EventObject
        from .ntenums import EVENT_TYPE
        receiver = window.gui
        remote_lparam, copy_back = self._marshal(receiver, window, message, wparam, lparam, wide)
        event = EventObject(EVENT_TYPE.NotificationEvent, False)
        handle = dp.handles.new(event)
        reply = {"event": event, "value": 0}
        receiver.sent.setdefault(window.thread, deque()).append((window.hwnd, message, wparam, remote_lparam, reply))
        receiver._wake(window.thread)
        self._record("send_message_to_process", hwnd=hex(window.hwnd), message=hex(message), pid=receiver.dp.process_id)
        wait = dp.modules.resolve_export("kernelbase.dll", "WaitForSingleObject").address

        def answered(_):
            dp.handles.close(handle)
            if copy_back is not None:
                copy_back()
            return reply["value"]
        return Invoke(wait, [handle, 0xFFFFFFFF], answered)

    def _handle_sent(self, thread: int, then):
        """A message another process sent to this thread: its window procedure, then the answer."""
        hwnd, message, wparam, lparam, reply = self.sent[thread].popleft()

        def answer(value):
            reply["value"] = value
            reply["event"].signalled = True
            world = self.world
            if world is not None:
                world.signal_all()
            return then()
        window = self.windows.get(hwnd)
        if window is None or not window.alive or not window.wndproc:
            return answer(self.def_window_proc(self.dp, hwnd, message, wparam, lparam) if window else 0)
        return self._wndproc_call(window, message, wparam, lparam, answer)

    def process_exited(self):
        """The process of this Gui ended: its windows are gone, the senders waiting for them get 0."""
        for window in list(self.windows.values()):
            if window.gui is self:
                window.alive = False
        for queue in self.sent.values():
            while queue:
                _, _, _, _, reply = queue.popleft()
                reply["event"].signalled = True

    def post_quit_message(self, dp, code):
        self.quit[dp.thread_id] = code & 0xFFFFFFFF
        return 0

    def _next_message(self, thread: int, remove: bool, idle_allowed: bool):
        """The next message of the thread (hwnd, message, wparam, lparam), a time to wait (int, in
        100 ns), or None."""
        queue = self._queue(thread)
        if queue:
            return queue.popleft() if remove else queue[0]
        if thread in self.quit:
            code = self.quit.pop(thread) if remove else self.quit[thread]
            return (0, WM_QUIT, code, 0)
        now = self._now()
        mine = [t for t in self.timers if t.thread == thread]
        due = [t for t in mine if t.due <= now]
        if due:
            timer = min(due, key=lambda t: t.due)
            if remove:
                timer.due = now + timer.period
            return (timer.hwnd, WM_TIMER, timer.id, timer.proc)
        if mine:
            return min(t.due for t in mine) - now
        if not idle_allowed:
            return None
        if self._others_alive() and not self.idle_waited.get(thread):
            return "wait"  # another process may post or send something: wait for it a while
        # nothing will come: the simulated user closes the thread's windows, then ends the session
        windows = [w for w in self.windows.values() if w.alive and w.thread == thread and not w.parent]
        if windows and thread not in self.closed:
            self.closed.add(thread)
            for window in windows:
                self._record("user_closes_window", hwnd=hex(window.hwnd), title=window.title)
                queue.append((window.hwnd, WM_CLOSE, 0, 0))
            return queue.popleft() if remove else queue[0]
        self._record("session_ends")
        return (0, WM_QUIT, 0, 0)

    def get_message(self, dp, msg, hwnd, first, last, wide):
        thread = dp.thread_id
        if self.sent.get(thread):
            return self._handle_sent(thread, lambda: self.get_message(dp, msg, hwnd, first, last, wide))
        found = self._next_message(thread, True, True)
        if found == "wait":
            # nothing yet: wait up to 2 s (virtual) for a message of another process
            event, handle = self._queue_event(thread)
            wait = dp.modules.resolve_export("kernelbase.dll", "WaitForSingleObject").address

            def waited(status):
                if (status & 0xFFFFFFFF) == 0x102:  # WAIT_TIMEOUT: still nothing
                    self.idle_waited[thread] = True
                return self.get_message(dp, msg, hwnd, first, last, wide)
            return Invoke(wait, [handle, 2000], waited)
        self.idle_waited[thread] = False
        if isinstance(found, int):
            # wait for the next timer, or for a message posted meanwhile (by another thread or process):
            # the program waits on the queue's event, the other threads run meanwhile
            _, handle_event = self._queue_event(dp.thread_id)
            wait = dp.modules.resolve_export("kernelbase.dll", "WaitForSingleObject").address
            milliseconds = max(1, found // 10_000)
            return Invoke(wait, [handle_event, milliseconds], lambda _: self.get_message(dp, msg, hwnd, first, last, wide))
        handle, message, wparam, lparam = found
        self._write_msg(msg, handle, message, wparam, lparam)
        return 0 if message == WM_QUIT else 1

    def peek_message(self, dp, msg, hwnd, first, last, flags, wide):
        thread = dp.thread_id
        if self.sent.get(thread):
            return self._handle_sent(thread, lambda: self.peek_message(dp, msg, hwnd, first, last, flags, wide))
        remove = bool(flags & 1)
        found = self._next_message(thread, remove, self.idle_peeks.get(thread, 0) > 200)
        if found == "wait":
            found = None
        if found is None or isinstance(found, int):
            self.idle_peeks[thread] = self.idle_peeks.get(thread, 0) + 1
            return 0
        self.idle_peeks[thread] = 0
        handle, message, wparam, lparam = found
        self._write_msg(msg, handle, message, wparam, lparam)
        return 1

    def dispatch_message(self, dp, msg, wide):
        hwnd, message, wparam, lparam = self._read_msg(msg)
        if message == WM_TIMER and lparam:
            return Invoke(lparam, [hwnd, WM_TIMER, wparam, (self._now() // 10_000) & 0xFFFFFFFF])
        window = self.windows.get(hwnd)
        if window is None or not window.alive:
            return 0
        if not window.wndproc:
            return self.def_window_proc(dp, hwnd, message, wparam, lparam)
        return self._wndproc_call(window, message, wparam, lparam)

    def set_timer(self, dp, hwnd, timer_id, elapse, proc):
        if not hwnd or not timer_id:
            self.next_timer += 1
            timer_id = timer_id or self.next_timer
        period = max(1, elapse & 0xFFFFFFFF) * 10_000
        self.timers = [t for t in self.timers if not (t.hwnd == hwnd and t.id == timer_id)]
        self.timers.append(Timer(self._now() + period, period, hwnd, timer_id, proc, dp.thread_id))
        return timer_id

    def kill_timer(self, dp, hwnd, timer_id):
        before = len(self.timers)
        self.timers = [t for t in self.timers if not (t.hwnd == hwnd and t.id == timer_id)]
        return 1 if len(self.timers) != before else 0

    # ------------------------------------------------------------------ dialogs
    def dialog_box(self, dp, instance, template, parent, dlgproc, param, wide):
        hwnd = self._handle()
        window = Window(hwnd, "#32770", dlgproc, "", 0, 0, parent, dp.thread_id, wide, dialog=True)
        window.gui = self
        self.windows[hwnd] = window
        self._record("dialog_box", hwnd=hex(hwnd), template=hex(template))
        if not dlgproc:
            return IDOK

        def finish(_):
            window.alive = False
            return window.dialog_result if window.dialog_result is not None else IDOK

        def after_init(_):
            if window.dialog_result is not None:
                return finish(0)
            self._record("user_clicks_ok", hwnd=hex(hwnd))
            return Invoke(dlgproc, [hwnd, WM_COMMAND, IDOK, 0], finish)
        return Invoke(dlgproc, [hwnd, WM_INITDIALOG, 0, param], after_init)

    def end_dialog(self, dp, hwnd, result):
        window = self.windows.get(hwnd)
        if window is not None:
            window.dialog_result = result
        return 1

    # ------------------------------------------------------------------ painting, text
    def draw_text(self, dp, hdc, text, length, *rest, wide):
        length = to_signed(length)
        value = read_string(dp, text, wide) if length < 0 else (
            bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace") if wide else bytes(dp.read(text, length)).decode("mbcs", "replace"))
        self._record("draw_text", text=value)
        if rest and rest[0]:
            rect = struct.unpack("<iiii", bytes(dp.read(rest[0], 16)))
            self.drawing.draw_text(hdc, value, rect, rest[1] & 0xFFFFFFFF if len(rest) > 1 else 0)
        return 16

    def text_out(self, dp, hdc, x, y, text, length, wide):
        value = bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace") if wide else bytes(dp.read(text, length)).decode("mbcs", "replace")
        self._record("draw_text", text=value)
        self.drawing.text(hdc, to_signed(x), to_signed(y), value)
        return 1

    def ext_text_out(self, dp, hdc, x, y, options, rect, text, length, dx, wide):
        box = struct.unpack("<iiii", bytes(dp.read(rect, 16))) if rect else None
        if box is not None and options & 2:  # ETO_OPAQUE
            dc = self.drawing.dcs.get(hdc)
            self.drawing.fill(hdc, box, dc.background if dc else 0xFFFFFF)
        if text:
            value = bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace") if wide else bytes(dp.read(text, length)).decode("mbcs", "replace")
            self._record("draw_text", text=value)
            self.drawing.text(hdc, to_signed(x), to_signed(y), value)
        return 1

    def begin_paint(self, dp, hwnd, paint):
        hdc = self.drawing.new_dc(hwnd)
        window = self.windows.get(hwnd)
        left, top, right, bottom = window.rect if window else (0, 0, 640, 480)
        rect = struct.pack("<iiii", 0, 0, right - left, bottom - top)
        if dp._x64:
            dp.write(paint, struct.pack("<QI", hdc, 0) + rect)
        else:
            dp.write(paint, struct.pack("<II", hdc, 0) + rect)
        return hdc

    def text_extent(self, dp, hdc, text, length, size, wide):
        value = bytes(dp.read(text, 2 * length)).decode("utf-16-le", "replace") if wide else bytes(dp.read(text, length)).decode("mbcs", "replace")
        width, height = self.drawing.text_extent(hdc, value)
        dp.write(size, struct.pack("<ii", width, height))
        return 1

    def dc_value(self, hdc, name, value):
        dc = self.drawing.dcs.get(hdc)
        if dc is None:
            return 0
        previous = getattr(dc, name)
        setattr(dc, name, value)
        return previous if not isinstance(previous, bool) else (1 if previous else 2)

    def move_to(self, dp, hdc, x, y, point):
        dc = self.drawing.dcs.get(hdc)
        if dc is not None:
            if point:
                dp.write(point, struct.pack("<ii", *dc.position))
            self.drawing.move_to(hdc, to_signed(x), to_signed(y))
        return 1

    def save_screenshots(self):
        """Images of the top-level windows not saved yet (the end of the run)."""
        for window in list(self.windows.values()):
            if window.alive:
                self.drawing.save_window(window)
        return list(self.drawing.saved)


import struct  # noqa: E402 (used by the methods above)


def install(dp):
    gui = Gui(dp)
    dp.gui = gui
    hooks = dp.apihooks
    for wide, suffix in ((True, "W"), (False, "A")):
        w = wide
        hooks.register("user32.dll", "MessageBoxTimeout" + suffix, 6, lambda dp, *a, w=w: gui.message_box(dp, *a, w))
        hooks.register("user32.dll", "MessageBoxIndirect" + suffix, 1, lambda dp, p, w=w: gui.message_box_indirect(dp, p, w))
        hooks.register("user32.dll", "RegisterClassEx" + suffix, 1, lambda dp, c, w=w: gui.register_class_ex(dp, c, w))
        hooks.register("user32.dll", "RegisterClass" + suffix, 1, lambda dp, c, w=w: gui.register_class(dp, c, w))
        hooks.register("user32.dll", "CreateWindowEx" + suffix, 12, lambda dp, *a, w=w: gui.create_window(dp, *a, w))
        hooks.register("user32.dll", "DefWindowProc" + suffix, 4, lambda dp, *a: gui.def_window_proc(dp, *a))
        hooks.register("user32.dll", "GetMessage" + suffix, 4, lambda dp, *a, w=w: gui.get_message(dp, *a, w))
        hooks.register("user32.dll", "PeekMessage" + suffix, 5, lambda dp, *a, w=w: gui.peek_message(dp, *a, w))
        hooks.register("user32.dll", "DispatchMessage" + suffix, 1, lambda dp, m, w=w: gui.dispatch_message(dp, m, w))
        hooks.register("user32.dll", "PostMessage" + suffix, 4, lambda dp, *a: gui.post_message(dp, *a))
        hooks.register("user32.dll", "PostThreadMessage" + suffix, 4, lambda dp, *a: gui.post_thread_message(dp, *a))
        hooks.register("user32.dll", "SendMessage" + suffix, 4, lambda dp, *a, w=w: gui.send_message(dp, *a, w))
        hooks.register("user32.dll", "SendNotifyMessage" + suffix, 4, lambda dp, *a: gui.send_notify(dp, *a))
        hooks.register("user32.dll", "SendMessageTimeout" + suffix, 7, lambda dp, *a, w=w: gui.send_message_timeout(dp, *a, w))
        hooks.register("user32.dll", "FindWindowEx" + suffix, 4,
                       lambda dp, p, a, c, t, w=w: gui.find_window(dp, c, t, w, p, a, True))
        hooks.register("user32.dll", "RegisterWindowMessage" + suffix, 1, lambda dp, n, w=w: gui.register_window_message(dp, n, w))
        hooks.register("user32.dll", "SetWindowText" + suffix, 2, lambda dp, h, t, w=w: gui.set_window_text(dp, h, t, w))
        hooks.register("user32.dll", "GetWindowText" + suffix, 3, lambda dp, h, b, n, w=w: gui.get_window_text(dp, h, b, n, w))
        hooks.register("user32.dll", "FindWindow" + suffix, 2, lambda dp, c, t, w=w: gui.find_window(dp, c, t, w))
        hooks.register("user32.dll", "DialogBoxParam" + suffix, 5, lambda dp, *a, w=w: gui.dialog_box(dp, *a, w))
        hooks.register("user32.dll", "DialogBoxIndirectParam" + suffix, 5, lambda dp, *a, w=w: gui.dialog_box(dp, *a, w))
        hooks.register("user32.dll", "DrawText" + suffix, 5, lambda dp, *a, w=w: gui.draw_text(dp, *a, wide=w))
        hooks.register("user32.dll", "DrawTextEx" + suffix, 6, lambda dp, *a, w=w: gui.draw_text(dp, *a, wide=w))
        hooks.register("gdi32.dll", "TextOut" + suffix, 5, lambda dp, *a, w=w: gui.text_out(dp, *a, w))
        hooks.register("gdi32.dll", "ExtTextOut" + suffix, 8, lambda dp, *a, w=w: gui.ext_text_out(dp, *a, w))
        hooks.register("user32.dll", "LoadIcon" + suffix, 2, lambda dp, *a: gui._handle())
        hooks.register("user32.dll", "LoadCursor" + suffix, 2, lambda dp, *a: gui._handle())
        # LoadImage: a bitmap (IMAGE_BITMAP, from the resources or a file with LR_LOADFROMFILE) is drawn
        hooks.register("user32.dll", "LoadImage" + suffix, 6, lambda dp, inst, name, kind, cx, cy, flags, w=w: (
            gui.drawing.load_bitmap(dp, inst, name, bool(flags & 0x10), w) if kind == 0 else gui._handle()))
        for name in (("GetWindowLongPtr", "GetWindowLong") if dp._x64 else ("GetWindowLong",)):
            hooks.register("user32.dll", name + suffix, 2, lambda dp, h, i: gui.window_long(dp, h, i))
        for name in (("SetWindowLongPtr", "SetWindowLong") if dp._x64 else ("SetWindowLong",)):
            hooks.register("user32.dll", name + suffix, 3, lambda dp, h, i, v: gui.window_long(dp, h, i, v))
    hooks.register("user32.dll", "DestroyWindow", 1, lambda dp, h: gui.destroy_window(dp, h))
    hooks.register("user32.dll", "GetWindowThreadProcessId", 2, lambda dp, h, p: gui.window_thread_process(dp, h, p))
    hooks.register("user32.dll", "ShowWindow", 2, lambda dp, h, c: gui.show_window(dp, h, c))
    hooks.register("user32.dll", "UpdateWindow", 1, lambda dp, h: gui.update_window(dp, h))
    hooks.register("user32.dll", "InvalidateRect", 3, lambda dp, *a: gui.invalidate(dp, *a))
    draw = gui.drawing
    hooks.register("gdi32.dll", "SetTextColor", 2, lambda dp, h, c: gui.dc_value(h, "text_color", c & 0xFFFFFF))
    hooks.register("gdi32.dll", "SetBkColor", 2, lambda dp, h, c: gui.dc_value(h, "background", c & 0xFFFFFF))
    hooks.register("gdi32.dll", "SetBkMode", 2, lambda dp, h, m: gui.dc_value(h, "transparent", m == 1))
    hooks.register("gdi32.dll", "CreateSolidBrush", 1, lambda dp, c: draw.new_object(GdiObject("brush", c & 0xFFFFFF)))
    hooks.register("gdi32.dll", "CreatePen", 3, lambda dp, s, w, c: draw.new_object(GdiObject("pen", None if s == 5 else c & 0xFFFFFF, max(1, w))))
    hooks.register("gdi32.dll", "GetStockObject", 1, lambda dp, i: STOCK_BASE + i if i in (0, 1, 2, 3, 4, 5, 6, 7, 8, 18, 19) else draw.new_object(GdiObject("font")))
    hooks.register("gdi32.dll", "SelectObject", 2, lambda dp, h, o: draw.select(h, o))
    hooks.register("gdi32.dll", "DeleteObject", 1, lambda dp, o: 1)
    hooks.register("gdi32.dll", "Rectangle", 5, lambda dp, h, l, t, r, b: (draw.rectangle(h, tuple(to_signed(v) for v in (l, t, r, b))), 1)[1])
    hooks.register("gdi32.dll", "Ellipse", 5, lambda dp, h, l, t, r, b: (draw.rectangle(h, tuple(to_signed(v) for v in (l, t, r, b)), True), 1)[1])
    hooks.register("gdi32.dll", "MoveToEx", 4, lambda dp, h, x, y, p: gui.move_to(dp, h, x, y, p))
    hooks.register("gdi32.dll", "LineTo", 3, lambda dp, h, x, y: (draw.line_to(h, to_signed(x), to_signed(y)), 1)[1])
    hooks.register("gdi32.dll", "SetPixel", 4, lambda dp, h, x, y, c: (draw.pixel(h, to_signed(x), to_signed(y), c & 0xFFFFFF), c)[1])
    for wide, suffix in ((True, "W"), (False, "A")):
        hooks.register("gdi32.dll", "GetTextExtentPoint32" + suffix, 4, lambda dp, *a, w=wide: gui.text_extent(dp, *a, w))
        hooks.register("gdi32.dll", "CreateFont" + suffix, 14, lambda dp, height, *a: draw.new_object(GdiObject("font", size=abs(to_signed(height)) or 15)))
        hooks.register("gdi32.dll", "CreateFontIndirect" + suffix, 1, lambda dp, lf: draw.new_object(GdiObject("font", size=abs(to_signed(dp.read_ulong(lf))) or 15)))
    hooks.register("user32.dll", "FillRect", 3, lambda dp, h, r, b: (draw.fill(h, struct.unpack("<iiii", bytes(dp.read(r, 16))), draw.brush_color(b)), 1)[1])
    hooks.register("user32.dll", "FrameRect", 3, lambda dp, h, r, b: (draw.fill(h, struct.unpack("<iiii", bytes(dp.read(r, 16))), draw.brush_color(b), True), 1)[1])
    hooks.register("user32.dll", "GetSysColorBrush", 1, lambda dp, i: (i & 0x1F) + 1)
    hooks.register("user32.dll", "GetSysColor", 1, lambda dp, i: SYSTEM_COLORS.get(i, 0xF0F0F0))
    hooks.register("user32.dll", "SetForegroundWindow", 1, lambda dp, h: 1)
    hooks.register("user32.dll", "IsWindow", 1, lambda dp, h: int(h in gui.windows and gui.windows[h].alive))
    hooks.register("user32.dll", "IsWindowVisible", 1, lambda dp, h: int(h in gui.windows and gui.windows[h].alive))
    hooks.register("user32.dll", "TranslateMessage", 1, lambda dp, m: 0)
    hooks.register("user32.dll", "PostQuitMessage", 1, lambda dp, c: gui.post_quit_message(dp, c))
    hooks.register("user32.dll", "SetTimer", 4, lambda dp, *a: gui.set_timer(dp, *a))
    hooks.register("user32.dll", "KillTimer", 2, lambda dp, *a: gui.kill_timer(dp, *a))
    hooks.register("user32.dll", "EndDialog", 2, lambda dp, *a: gui.end_dialog(dp, *a))
    hooks.register("user32.dll", "GetClientRect", 2, lambda dp, h, r: gui.get_rect(dp, h, r, True))
    hooks.register("user32.dll", "GetWindowRect", 2, lambda dp, h, r: gui.get_rect(dp, h, r, False))
    hooks.register("user32.dll", "GetDC", 1, lambda dp, h: gui.drawing.new_dc(h))
    hooks.register("user32.dll", "GetWindowDC", 1, lambda dp, h: gui.drawing.new_dc(h))
    hooks.register("user32.dll", "ReleaseDC", 2, lambda dp, *a: 1)
    hooks.register("user32.dll", "BeginPaint", 2, lambda dp, h, p: gui.begin_paint(dp, h, p))
    hooks.register("user32.dll", "EndPaint", 2, lambda dp, *a: 1)
    hooks.register("user32.dll", "GetDesktopWindow", 0, lambda dp: 0x10010)
    hooks.register("user32.dll", "GetForegroundWindow", 0,
                   lambda dp: max((w.hwnd for w in gui.windows.values() if w.alive), default=0))
    hooks.register("user32.dll", "MessageBeep", 1, lambda dp, t: 1)
    hooks.register("user32.dll", "GetCursorPos", 1, lambda dp, p: (dp.write(p, struct.pack("<ii", 100, 100)), 1)[1])
    hooks.register("user32.dll", "SetCursorPos", 2, lambda dp, *a: 1)
    hooks.register("user32.dll", "GetAsyncKeyState", 1, lambda dp, k: 0)
    hooks.register("user32.dll", "GetKeyState", 1, lambda dp, k: 0)
    metrics = {0: 1920, 1: 1080, 4: 23, 5: 1, 6: 1, 16: 1920, 17: 1040, 32: 8, 33: 8, 43: 3, 80: 1}
    hooks.register("user32.dll", "GetSystemMetrics", 1, lambda dp, i: metrics.get(to_signed(i), 0))
    from .gui_draw import register as _register_drawing
    _register_drawing(hooks, gui)
    return gui
