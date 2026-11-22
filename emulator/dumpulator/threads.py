# Fathom addition to dumpulator: several threads, synchronization objects and a virtual clock.
# Boost Software License 1.0, like the rest of dumpulator (LICENSE.dumpulator).
#
# The thread of the dump runs first. Threads the emulated code creates (NtCreateThreadEx) get a
# stack, a TEB copied from the dumped thread's and an initial context that enters ntdll's
# LdrInitializeThunk, like on Windows (TLS, DLL_THREAD_ATTACH), then RtlUserThreadStart. The other
# threads of the dump stay where they are: they wait in the kernel.
#
# Several processors: up to `cpus` threads are on a processor at once (the number of processors of
# the dumped machine by default) and run interleaved in short slices of random length (a fixed seed:
# the same run every time), as on a multiprocessor where their instructions mix; a race between two
# threads happens as it does natively. A thread keeps its processor for QUANTUM instructions of its
# own, then lets a thread that waits for a processor run. A thread leaves its processor when it waits
# for something not signaled (events, mutants, semaphores, threads, NtWaitForAlertByThreadId of
# critical sections / SRW locks / condition variables, keyed events, NtDelayExecution). When every
# thread waits, the clock jumps to the first timeout; when none has one, the emulation stops
# (deadlock).
#
# Time is virtual: it advances with the emulated instructions and jumps over sleeps, and
# KUSER_SHARED_DATA (GetTickCount, the interrupt and system time) follows it.

import ctypes
import random
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .handles import AbstractObject, EventObject
from .native import *

STATUS_TIMEOUT = 0x102
STATUS_ALERTED = 0x101
STATUS_PENDING = 0x103
STATUS_ABANDONED = 0x80
STATUS_MUTANT_NOT_OWNED = 0xC0000046
STATUS_SEMAPHORE_LIMIT_EXCEEDED = 0xC0000047
STATUS_THREAD_IS_TERMINATING = 0xC000004B

KUSER_SHARED_DATA = 0x7FFE0000

# TEB offsets (Windows 10 / 11), checked against the dumped thread's TEB when a thread is created
_TEB64 = dict(ptr=8, exception_list=0x0, stack_base=0x8, stack_limit=0x10, version=0x20, self=0x30, pid=0x40, tid=0x48,
              peb=0x60, locale=0x108, actx_stack=0x290, actx=0x2C8, static_string=0x1258, static_buffer=0x1268,
              dealloc=0x1478, wow_teb_offset=0x180C)
_TEB32 = dict(ptr=4, exception_list=0x0, stack_base=0x4, stack_limit=0x8, version=0x10, self=0x18, pid=0x20, tid=0x24,
              peb=0x30, locale=0xC4, actx_stack=0x184, actx=0x1A8, static_string=0xBF8, static_buffer=0xC00,
              dealloc=0xE0C, wow_teb_offset=0xFDC)


@dataclass
class Wait:
    objects: List[AbstractObject] = field(default_factory=list)
    wait_all: bool = False
    deadline: Optional[int] = None      # virtual time (100 ns), None: infinite
    alert: bool = False                 # NtWaitForAlertByThreadId
    keyed: Optional[tuple] = None       # (keyed event, key, "wait" | "release")
    retry: Optional[tuple] = None       # (address of the syscall instruction, eax): the call runs again


class EmulatedThread:
    def __init__(self, tid: int, teb: int, state: str):
        self.tid = tid
        self.teb = teb              # the TEB the thread's code uses (the 32-bit one in a WoW64 process)
        self.state = state          # running, ready, waiting, suspended, terminated, frozen (dumped, not run)
        self.context = None         # unicorn context while not running
        self.wait: Optional[Wait] = None
        self.alerted = False        # NtAlertThreadByThreadId before the thread waits for it
        self.suspend_count = 0
        self.exit_code: Optional[int] = None
        self.start = 0
        self.object: Optional["ThreadObject2"] = None
        self.quantum_left = 0

    def __repr__(self):
        return f"thread {self.tid} ({self.state})"


@dataclass
class ThreadObject2(AbstractObject):
    """A thread handle: signaled when the thread has terminated."""
    thread: EmulatedThread

    def __str__(self):
        return f"ThreadObject(tid={self.thread.tid}, {self.thread.state})"


@dataclass
class MutantObject(AbstractObject):
    owner: Optional[int] = None
    count: int = 0
    abandoned: bool = False


@dataclass
class SemaphoreObject(AbstractObject):
    count: int = 0
    maximum: int = 1


@dataclass
class KeyedEventObject(AbstractObject):
    pass


class Scheduler:
    QUANTUM = 20000         # instructions a thread runs on its processor before a waiting thread gets it
    SLICE = (40, 400)       # instructions between two switches of the threads on processors
    NS_PER_INSTRUCTION = 1  # virtual time per emulated instruction (1 ns: 1 GHz, one instruction per cycle)

    def __init__(self, dp):
        self.dp = dp
        self.threads: Dict[int, EmulatedThread] = {}
        self.ready: deque = deque()
        self.stop_reason: Optional[str] = None
        main = EmulatedThread(dp.thread_id, dp.teb, "running")
        self.threads[main.tid] = main
        self.current = main
        highest = main.tid
        for thread in dp._minidump.threads.threads:
            highest = max(highest, thread.ThreadId)
            if thread.ThreadId != main.tid:
                self.threads[thread.ThreadId] = EmulatedThread(thread.ThreadId, thread.Teb, "frozen")
        self.next_tid = (highest + 4) & ~3
        # virtual clock, from KUSER_SHARED_DATA
        self.interrupt_time = self._read_ksystem_time(0x8)
        self.system_time = self._read_ksystem_time(0x14)
        self.start_interrupt_time = self.interrupt_time
        self.tick_multiplier = dp.read_ulong(KUSER_SHARED_DATA + 0x4) or 0x0FA00000
        self.qpc_frequency = struct.unpack("<Q", bytes(dp.read(KUSER_SHARED_DATA + 0x300, 8)))[0] or 10_000_000
        self._clock_rest = 0
        # processors: the number of the dumped machine (PEB.NumberOfProcessors)
        try:
            self.cpus = max(1, dp.read_ulong(dp.peb + (0xB8 if dp._x64 else 0x64)))
        except Exception:
            self.cpus = 1
        self.on_cpu: List[EmulatedThread] = [main]
        main.quantum_left = self.QUANTUM
        self.rng = random.Random(1)
        # several processes (processes.py): a child whose threads all wait pauses until another
        # process changes something; `budget` instructions, then the emulation returns
        self.paused = False
        self.budget: Optional[int] = None
        self.yield_requested = False

    # ------------------------------------------------------------------ time
    def _read_ksystem_time(self, offset: int) -> int:
        low, high = struct.unpack("<II", bytes(self.dp.read(KUSER_SHARED_DATA + offset, 8)))
        return (high << 32) | low

    def _write_ksystem_time(self, offset: int, value: int):
        low, high = value & 0xFFFFFFFF, (value >> 32) & 0xFFFFFFFF
        self.dp.write(KUSER_SHARED_DATA + offset, struct.pack("<III", low, high, high))

    def advance(self, ticks: int):
        """Advance the virtual clock by ticks (100 ns) and update KUSER_SHARED_DATA."""
        if ticks <= 0:
            return
        self.interrupt_time += ticks
        self.system_time += ticks
        try:
            self._write_ksystem_time(0x8, self.interrupt_time)
            self._write_ksystem_time(0x14, self.system_time)
            milliseconds = self.interrupt_time // 10_000
            tick_count = (milliseconds << 24) // self.tick_multiplier
            self.dp.write(KUSER_SHARED_DATA + 0x320, struct.pack("<IIi", tick_count & 0xFFFFFFFF, tick_count >> 32, tick_count >> 32))
            self.dp.write(KUSER_SHARED_DATA + 0x0, struct.pack("<I", tick_count & 0xFFFFFFFF))
        except Exception:
            pass

    def advance_instructions(self, count: int):
        """The clock after count instructions run: with several threads on processors, they ran in
        parallel, so the time of one processor."""
        parallel = max(1, len(self.on_cpu))
        ns = count * self.NS_PER_INSTRUCTION + self._clock_rest
        ticks, self._clock_rest = divmod(ns, 100 * parallel)
        self.advance(ticks)

    def performance_counter(self) -> int:
        return self.interrupt_time * self.qpc_frequency // 10_000_000

    def deadline(self, timeout_ptr) -> Optional[int]:
        """The deadline of a timeout (P[LARGE_INTEGER]): None when infinite (NULL pointer)."""
        if timeout_ptr == 0:
            return None
        value = struct.unpack("<q", bytes(self.dp.read(timeout_ptr.ptr, 8)))[0]
        if value < 0:   # relative
            return self.interrupt_time - value
        return self.interrupt_time + max(0, value - self.system_time)  # absolute (system time)

    # ------------------------------------------------------------------ threads
    def running_threads(self) -> List[EmulatedThread]:
        return [t for t in self.threads.values() if t.state not in ("terminated", "frozen")]

    def others_ready(self) -> bool:
        return bool(self.ready) or len(self.on_cpu) > 1

    def slice_length(self) -> int:
        """The instructions to run before the next switch."""
        world = getattr(self.dp, "world", None)
        if len(self.on_cpu) > 1 or (world is not None and world.fine()):
            # threads on several processors, or processes that share memory: short slices
            length = self.rng.randint(*self.SLICE)
        elif self.ready:
            length = max(1, self.current.quantum_left)
        else:
            length = self.QUANTUM
        if self.budget is not None:
            length = min(length, max(1, self.budget))
        return length

    def find(self, tid: int) -> Optional[EmulatedThread]:
        return self.threads.get(tid)

    def thread_of_handle(self, handle: int) -> Optional[EmulatedThread]:
        dp = self.dp
        if handle in (0, dp.NtCurrentThread() & dp.addr_mask, dp.NtCurrentThread()):
            return self.current
        try:
            obj = dp.handles.get(handle, None)
        except Exception:
            return None
        if isinstance(obj, ThreadObject2):
            return obj.thread
        return None

    def add_thread(self, thread: EmulatedThread, suspended: bool):
        self.threads[thread.tid] = thread
        thread.object = ThreadObject2(thread)
        if suspended:
            thread.state = "suspended"
            thread.suspend_count = 1
        else:
            self._make_ready(thread)

    def _make_ready(self, thread: EmulatedThread):
        """A thread can run: on a free processor, else it waits for one."""
        thread.state = "ready"
        thread.quantum_left = self.QUANTUM
        if thread in self.on_cpu or thread in self.ready:
            return
        if len(self.on_cpu) < self.cpus:
            self.on_cpu.append(thread)
        else:
            self.ready.append(thread)

    def _release(self, thread: EmulatedThread):
        """A thread leaves its processor; the threads that wait for one take the free processors."""
        if thread in self.on_cpu:
            self.on_cpu.remove(thread)
        while self.ready and len(self.on_cpu) < self.cpus:
            waiting = self.ready.popleft()
            if waiting.state == "ready":
                waiting.quantum_left = self.QUANTUM
                self.on_cpu.append(waiting)

    def _make_current(self, thread: EmulatedThread):
        if thread is not self.current:
            self.dp.info(f"[thread {self.current.tid} -> {thread.tid}]")
        self.current = thread
        thread.state = "running"
        self.dp.thread_id = thread.tid
        self.dp.teb = thread.teb

    # ------------------------------------------------------------------ objects
    def _signaled(self, obj, tid: int) -> bool:
        if isinstance(obj, EventObject):
            return bool(obj.signalled)
        if isinstance(obj, ThreadObject2):
            return obj.thread.state == "terminated"
        if isinstance(obj, MutantObject):
            return obj.owner is None or obj.owner == tid
        if isinstance(obj, SemaphoreObject):
            return obj.count > 0
        signaled = getattr(obj, "fathom_signaled", None)
        if signaled is not None:  # a child process or thread (processes.py)
            return signaled()
        return False  # process handle, objects of the dump: never signaled

    def _acquire(self, obj, tid: int) -> int:
        if isinstance(obj, EventObject):
            if obj.event_type == EVENT_TYPE.SynchronizationEvent:
                obj.signalled = False
        elif isinstance(obj, MutantObject):
            obj.owner = tid
            obj.count += 1
            if obj.abandoned:
                obj.abandoned = False
                return STATUS_ABANDONED
        elif isinstance(obj, SemaphoreObject):
            obj.count -= 1
        return 0

    def _try_satisfy(self, thread: EmulatedThread, wait: Wait) -> Optional[int]:
        """The status of the wait if it is satisfied now (objects acquired), else None."""
        if wait.alert:
            if thread.alerted:
                thread.alerted = False
                return STATUS_ALERTED
            return None
        if wait.keyed is not None:
            return None
        if not wait.objects:
            return None
        if wait.wait_all:
            if all(self._signaled(o, thread.tid) for o in wait.objects):
                abandoned = 0
                for o in wait.objects:
                    abandoned |= self._acquire(o, thread.tid)
                return STATUS_ABANDONED if abandoned else 0
            return None
        for index, o in enumerate(wait.objects):
            if self._signaled(o, thread.tid):
                return index + self._acquire(o, thread.tid)
        return None

    # ------------------------------------------------------------------ waits (from system calls)
    def wait(self, objects, wait_all: bool, timeout_ptr) -> object:
        """Wait for objects: a status when satisfied at once (or a zero timeout), otherwise the
        current thread blocks and the result is the context switch to give the system call."""
        wait = Wait(list(objects), wait_all, self.deadline(timeout_ptr))
        status = self._try_satisfy(self.current, wait)
        if status is not None:
            return status
        if wait.deadline is not None and wait.deadline <= self.interrupt_time:
            return STATUS_TIMEOUT
        return self.block(wait)

    def wait_alert(self, timeout_ptr) -> object:
        wait = Wait(alert=True, deadline=self.deadline(timeout_ptr))
        status = self._try_satisfy(self.current, wait)
        if status is not None:
            return status
        if wait.deadline is not None and wait.deadline <= self.interrupt_time:
            return STATUS_TIMEOUT
        return self.block(wait)

    def alert(self, tid: int) -> int:
        thread = self.threads.get(tid)
        if thread is None or thread.state == "terminated":
            return STATUS_INVALID_PARAMETER
        if thread.state == "waiting" and thread.wait is not None and thread.wait.alert:
            self.wake(thread, STATUS_ALERTED)
        else:
            thread.alerted = True
        return STATUS_SUCCESS

    def keyed(self, keyed_event, key: int, kind: str, timeout_ptr) -> object:
        """NtWaitForKeyedEvent / NtReleaseKeyedEvent: a waiter and a releaser of the same key meet."""
        other = "release" if kind == "wait" else "wait"
        for thread in self.threads.values():
            w = thread.wait
            if thread.state == "waiting" and w is not None and w.keyed is not None \
                    and w.keyed[0] is keyed_event and w.keyed[1] == key and w.keyed[2] == other:
                self.wake(thread, STATUS_SUCCESS)
                return STATUS_SUCCESS
        wait = Wait(keyed=(keyed_event, key, kind), deadline=self.deadline(timeout_ptr))
        if wait.deadline is not None and wait.deadline <= self.interrupt_time:
            return STATUS_TIMEOUT
        return self.block(wait)

    def delay(self, timeout_ptr) -> object:
        deadline = self.deadline(timeout_ptr)
        if deadline is None or deadline <= self.interrupt_time:
            return self.yield_execution()
        return self.block(Wait(deadline=deadline))

    def yield_execution(self) -> object:
        if not self.ready and len(self.on_cpu) <= 1:
            return STATUS_SUCCESS

        def give_way(thread):
            thread.state = "ready"
            if self.ready:  # the processor goes to a thread that waits for one
                self._release(thread)
                self.ready.append(thread)
        return self.switch_from_syscall(give_way, STATUS_SUCCESS)

    def _requeue(self, thread: EmulatedThread):
        self._make_ready(thread)

    def signal(self):
        """An object changed state: wake the threads whose wait is now satisfied (in order)."""
        for thread in list(self.threads.values()):
            if thread.state == "waiting" and thread.wait is not None:
                status = self._try_satisfy(thread, thread.wait)
                if status is not None:
                    self.wake(thread, status)

    def wake(self, thread: EmulatedThread, status: int):
        wait = thread.wait
        thread.wait = None
        if wait is not None and wait.retry is not None and status != STATUS_TIMEOUT:
            # the system call runs again (a read of a pipe that has data now...)
            from unicorn.x86_const import UC_X86_REG_RIP, UC_X86_REG_EIP, UC_X86_REG_RAX
            thread.context.reg_write(UC_X86_REG_RIP if self.dp._x64 else UC_X86_REG_EIP, wait.retry[0])
            thread.context.reg_write(UC_X86_REG_RAX, wait.retry[1])
        else:
            self._set_result(thread, status)
        if thread.suspend_count:
            thread.state = "suspended"
        else:
            self._make_ready(thread)

    def _set_result(self, thread: EmulatedThread, status: int):
        from unicorn.x86_const import UC_X86_REG_RAX
        thread.context.reg_write(UC_X86_REG_RAX, status & 0xFFFFFFFF)

    # ------------------------------------------------------------------ switching
    def _post_syscall_state(self, status: int):
        """The registers of the current thread as after its system call returned (what
        dumpulator's syscall hook does when the call does not block)."""
        dp = self.dp
        regs = dp.regs
        cip = regs.cip
        regs.cax = status & 0xFFFFFFFF
        if dp._x64:
            regs.rcx = cip + 2
            regs.r11 = regs.eflags
        regs.cip = cip + 2

    def block_and_retry(self, objects, timeout_ptr=0) -> object:
        """The current thread waits until one of the objects is signaled (fathom_signaled), then its
        system call runs again: for a call that blocks until something happens (reading an empty
        pipe, waiting for a client), the implementation returns this instead of a status."""
        dp = self.dp
        wait = Wait(list(objects), False, self.deadline(timeout_ptr))
        wait.retry = (dp.regs.cip, dp.regs.cax & 0xFFFFFFFF)
        return self.block(wait)

    def block(self, wait: Wait) -> object:
        def save(thread):
            thread.state = "waiting"
            thread.wait = wait
        return self.switch_from_syscall(save, STATUS_PENDING)

    def terminate_current(self, exit_code: int) -> object:
        def save(t):
            t.state = "terminated"
            t.exit_code = exit_code & 0xFFFFFFFF
            self.signal()  # the threads waiting for this one, before the next one is chosen
        return self.switch_from_syscall(save, 0)

    def terminate_thread(self, thread: EmulatedThread, exit_code: int):
        if thread.state == "terminated":
            return
        if thread in self.ready:
            self.ready.remove(thread)
        self._release(thread)
        thread.state = "terminated"
        thread.wait = None
        thread.exit_code = exit_code & 0xFFFFFFFF
        self.signal()

    def switch_from_syscall(self, save_current, status: int) -> object:
        """Leave the current thread in a system call: its state becomes the one after the call
        (status in rax), save_current() sets what it does next, and the next thread runs. Returns
        the exception that makes dumpulator switch, or stops the emulation."""
        from .dumpulator import UnicornExceptionInfo, ExceptionType
        dp = self.dp
        current = self.current
        index = self.on_cpu.index(current) if current in self.on_cpu else 0
        self._post_syscall_state(status)
        current.context = dp._uc.context_save()
        save_current(current)
        if current.state in ("waiting", "terminated", "suspended"):
            self._release(current)
        nxt = self._pick_next(current, index)
        exception = UnicornExceptionInfo()
        exception.final = True
        if nxt is None:
            exception.type = ExceptionType.Terminate
            exception.context = current.context
            world = getattr(dp, "world", None)
            if world is not None and world.can_pause(dp) and not all(
                    t.state in ("terminated", "frozen") for t in self.threads.values()):
                # a child process whose threads all wait: it pauses until another process (its
                # parent...) changes what they wait for; the world resumes it
                self.paused = True
                return exception
            dp.stop(self._exit_code_without_threads())
            return exception
        self._make_current(nxt)
        exception.type = ExceptionType.ContextSwitch
        exception.context = nxt.context
        if dp._fathom_trace_switch is not None:
            dp._fathom_trace_switch()
        return exception

    def _exit_code_without_threads(self):
        if all(t.state in ("terminated", "frozen") for t in self.threads.values()):
            self.stop_reason = "every emulated thread has terminated"
            return None
        waiting = [t for t in self.threads.values() if t.state in ("waiting", "suspended")]
        self.stop_reason = "deadlock: every emulated thread waits (" + ", ".join(
            f"{t.tid}: {'suspended' if t.state == 'suspended' else self._describe(t.wait)}" for t in waiting) + ")"
        self.dp.error(self.stop_reason)
        return None

    @staticmethod
    def _describe(wait: Optional[Wait]) -> str:
        if wait is None:
            return "?"
        if wait.alert:
            return "alert by thread id"
        if wait.keyed is not None:
            return "keyed event"
        if wait.objects:
            return ("all of " if wait.wait_all else "") + ", ".join(type(o).__name__ for o in wait.objects)
        return "delay"

    def next_deadline(self) -> Optional[int]:
        """The first timeout of the waiting threads (virtual time), or None."""
        timed = [t.wait.deadline for t in self.threads.values()
                 if t.state == "waiting" and t.wait is not None and t.wait.deadline is not None]
        return min(timed) if timed else None

    def advance_to(self, when: int):
        """The clock moves on to `when` (if later); the waits whose timeout passed end."""
        if when > self.interrupt_time:
            self.advance(when - self.interrupt_time)
        for thread in list(self.threads.values()):
            wait = thread.wait
            if thread.state == "waiting" and wait is not None and wait.deadline is not None and wait.deadline <= self.interrupt_time:
                status = STATUS_SUCCESS if (wait.keyed is None and not wait.alert and not wait.objects) else STATUS_TIMEOUT
                self.wake(thread, status)

    def runnable(self) -> bool:
        """A thread can run now (after the objects it waits for are checked)."""
        self.signal()
        self._release(None)
        return bool(self.on_cpu)

    def _pick_next(self, after: Optional[EmulatedThread] = None, index: int = 0) -> Optional[EmulatedThread]:
        """The next thread to run: the thread on a processor after `after` (in turn). When no
        thread can run, the other emulated processes run (one of them may signal what this one
        waits for); when none can, the clock of every process jumps to the first timeout."""
        world = getattr(self.dp, "world", None)
        while True:
            self._release(None)  # free processors to the threads that wait for one
            if self.on_cpu:
                if after in self.on_cpu:
                    return self.on_cpu[(self.on_cpu.index(after) + 1) % len(self.on_cpu)]
                return self.on_cpu[index % len(self.on_cpu)]
            if world is not None:
                if world.wait_for_others(self.dp):
                    continue
                return None
            first = self.next_deadline()
            if first is None:
                return None
            self.advance_to(first)

    def preempt(self, executed: int = 0) -> bool:
        """A slice of the current thread is over (between two emu_start): the clock advances, and
        the next thread on a processor runs (or a thread waiting for a processor gets this one when
        the quantum is over). Returns True when another thread was switched in."""
        dp = self.dp
        current = self.current
        self.advance_instructions(executed)
        world = getattr(dp, "world", None)
        if world is not None:
            world.slice_done(dp, executed)
        if self.budget is not None:
            self.budget -= executed
            if self.budget <= 0:
                self.yield_requested = True
        current.quantum_left -= executed
        index = self.on_cpu.index(current) if current in self.on_cpu else 0
        if current.quantum_left <= 0:
            current.quantum_left = self.QUANTUM
            if self.ready:
                self._release(current)
                current.state = "ready"
                self.ready.append(current)
        if current in self.on_cpu and len(self.on_cpu) == 1:
            return False
        nxt = self._pick_next(current, index)
        if nxt is None or nxt is current:
            return False
        current.context = dp._uc.context_save()
        if current.state == "running":
            current.state = "ready"
        self._make_current(nxt)
        dp._uc.context_restore(nxt.context)
        if dp._fathom_trace_switch is not None:
            dp._fathom_trace_switch()
        return True

    def resume(self, thread: EmulatedThread) -> int:
        previous = thread.suspend_count
        if thread.suspend_count > 0:
            thread.suspend_count -= 1
            if thread.suspend_count == 0 and thread.state == "suspended":
                if thread.wait is not None:
                    thread.state = "waiting"
                else:
                    self._make_ready(thread)
        return previous

    def suspend(self, thread: EmulatedThread) -> int:
        previous = thread.suspend_count
        thread.suspend_count += 1
        if thread.state == "ready":
            if thread in self.ready:
                self.ready.remove(thread)
            self._release(thread)
            thread.state = "suspended"
        return previous

    # ------------------------------------------------------------------ thread creation
    def create_thread(self, start: int, argument: int, suspended: bool, stack_size: int) -> EmulatedThread:
        dp = self.dp
        from .memory import MemoryProtect, MemoryType
        tid = self.next_tid
        self.next_tid += 4
        x64 = dp._x64
        ptr = 8 if x64 else 4
        # stack
        size = max(stack_size, 0x100000)
        size = (size + 0xFFFF) & ~0xFFFF
        stack = dp.memory.find_free(size)
        dp.memory.reserve(stack, size, MemoryProtect.PAGE_READWRITE, MemoryType.MEM_PRIVATE, info=f"Stack (thread {tid})")
        dp.memory.commit(stack, size, MemoryProtect.PAGE_READWRITE)
        stack_top = stack + size
        # TEB: zeroed, with what the kernel sets in a new thread's TEB (MmCreateTeb); the few values
        # that are the same for every thread of the process come from the dumped thread's TEB
        main = self.threads[dp._fathom_initial_tid]
        if x64:
            template, size = main.teb, 0x2000
            parts = [(0, _TEB64, main.teb)]
        else:
            template, size = main.teb - 0x2000, 0x4000  # 64-bit TEB, then the 32-bit one (two pages each)
            parts = [(0, _TEB64, main.teb - 0x2000), (0x2000, _TEB32, main.teb)]
        region = (size + 0xFFFF) & ~0xFFFF
        new_base = dp.memory.find_free(region)
        dp.memory.reserve(new_base, region, MemoryProtect.PAGE_READWRITE, MemoryType.MEM_PRIVATE, info=f"TEB (thread {tid})")
        dp.memory.commit(new_base, size, MemoryProtect.PAGE_READWRITE)
        data = bytearray(size)
        teb = new_base if x64 else new_base + 0x2000
        pid = dp.process_id
        for offset, layout, old in parts:
            width = layout["ptr"]
            fmt = "<Q" if width == 8 else "<I"
            here = new_base + offset

            def put(field_name, value, size_=width):
                struct.pack_into({8: "<Q", 4: "<I", 2: "<H"}[size_], data, offset + layout[field_name], value & ((1 << (8 * size_)) - 1))

            def copy(at, length):
                data[offset + at:offset + at + length] = bytes(dp.read(old + at, length))

            native = layout is _TEB32 or x64   # the TEB the emulated code uses
            put("exception_list", 0xFFFFFFFF if width == 4 else 0)
            put("stack_base", stack_top if native else 0)
            put("stack_limit", stack if native else 0)
            put("dealloc", stack if native else 0)
            put("self", here)
            put("pid", pid)
            put("tid", tid)
            put("peb", dp.read_ptr(old + layout["peb"]) if width == dp.ptr_size() else struct.unpack(fmt, bytes(dp.read(old + layout["peb"], width)))[0])
            copy(layout["version"], width)        # NtTib.Version (FiberData of a thread that is not a fiber)
            copy(layout["locale"], 4)             # CurrentLocale
            copy(layout["wow_teb_offset"], 4)     # WowTebOffset
            # StaticUnicodeString: Length 0, MaximumLength, Buffer = StaticUnicodeBuffer
            unicode_string = layout["static_string"]
            struct.pack_into("<HH", data, offset + unicode_string, 0, 0x20A)
            struct.pack_into(fmt, data, offset + unicode_string + width, here + layout["static_buffer"])
            # ActivationContextStackPointer stays NULL: ntdll allocates the activation context stack of
            # a new thread (and frees it when the thread ends); only the first thread uses the one
            # inside its TEB
        dp.write(new_base, bytes(data))

        thread = EmulatedThread(tid, teb, "ready")
        thread.start = start
        thread.context = self._initial_context(thread, start, argument, stack_top, new_base)
        self.add_thread(thread, suspended)
        return thread

    def _initial_context(self, thread: EmulatedThread, start: int, argument: int, stack_top: int, teb_base: int):
        """A unicorn context that enters ntdll!LdrInitializeThunk(context, ntdll) with a CONTEXT whose
        instruction pointer is RtlUserThreadStart(start, argument), as Windows starts a thread."""
        dp = self.dp
        regs = dp.regs
        ntdll = dp.modules["ntdll.dll"]
        thunk = ntdll.find_export("LdrInitializeThunk").address
        user_start = ntdll.find_export("RtlUserThreadStart").address
        saved = dp._uc.context_save()
        try:
            if dp._x64:
                initial_rsp = ((stack_top - 0x100) & ~0xF) - 8
                context = (initial_rsp - 0x600) & ~0xF
                ctx = bytearray(0x4D0)
                struct.pack_into("<II", ctx, 0x30, 0x10000B, 0x1F80)                       # ContextFlags, MxCsr
                struct.pack_into("<6H", ctx, 0x38, 0x33, 0x2B, 0x2B, 0x53, 0x2B, 0x2B)     # Cs Ds Es Fs Gs Ss
                struct.pack_into("<I", ctx, 0x44, 0x200)                                    # EFlags
                struct.pack_into("<Q", ctx, 0x80, start)                                    # Rcx
                struct.pack_into("<Q", ctx, 0x88, argument)                                 # Rdx
                struct.pack_into("<Q", ctx, 0x98, initial_rsp)                              # Rsp
                struct.pack_into("<Q", ctx, 0xF8, user_start)                               # Rip
                struct.pack_into("<H", ctx, 0x100, 0x27F)                                   # FltSave.ControlWord
                struct.pack_into("<I", ctx, 0x118, 0x1F80)                                  # FltSave.MxCsr
                dp.write(context, bytes(ctx))
                for name in ("rax", "rbx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"):
                    regs[name] = 0
                regs.rsp = context - 8
                dp.write(context - 8, struct.pack("<Q", 0))
                regs.rcx = context
                regs.rdx = ntdll.base
                regs.rip = thunk
                regs.eflags = 0x202
                regs.gs_base = teb_base
            else:
                initial_esp = ((stack_top - 0x100) & ~0xF) - 0x10
                context = (initial_esp - 0x400) & ~0xF
                ctx = bytearray(0x2CC)
                struct.pack_into("<I", ctx, 0x0, 0x1000B)                                   # ContextFlags
                struct.pack_into("<IIII", ctx, 0x8C, 0x2B, 0x53, 0x2B, 0x2B)                # Gs Fs Es Ds
                struct.pack_into("<I", ctx, 0xA4, argument)                                 # Ebx
                struct.pack_into("<I", ctx, 0xB0, start)                                    # Eax
                struct.pack_into("<IIII", ctx, 0xB8, user_start, 0x23, 0x202, initial_esp)  # Eip Cs EFlags Esp
                struct.pack_into("<I", ctx, 0xC8, 0x2B)                                     # Ss
                struct.pack_into("<H", ctx, 0xCC, 0x27F)                                    # ExtendedRegisters: FCW
                struct.pack_into("<I", ctx, 0xCC + 0x18, 0x1F80)                            # MxCsr
                dp.write(context, bytes(ctx))
                esp = context - 0x10
                dp.write(esp, struct.pack("<III", 0, context, ntdll.base))
                for name in ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp"):
                    regs[name] = 0
                regs.esp = esp
                regs.eip = thunk
                regs.eflags = 0x202
                regs.fs_base = thread.teb
                regs.gs_base = thread.teb - 0x2000
            return dp._uc.context_save()
        finally:
            dp._uc.context_restore(saved)
