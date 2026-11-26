"""Fathom for Binary Ninja: Binary Ninja and the Fathom debugger side by side (Binary Ninja's paid editions:
the Free edition has no plugins; Fathom's `binja` command works with every edition).

In Fathom: `pybridge.start` (pybridge listens on 127.0.0.1:27041). In Binary Ninja, menu Plugins > Fathom:
  Connect                   the module of this view in the debuggee (same file name) and the address mapping
  Follow the debugger       the view follows the instruction pointer of the debuggee (again: stop)
  Show in Fathom            Fathom's disassembly goes to the selected address
  Set / Delete breakpoint   at the selected address
  Run to here               the debuggee runs to the selected address
  Import a trace            a trace exported by Fathom (TraceExport ..., json): executed instructions
                            highlighted, a tag with their count
"""
from __future__ import annotations

import binaryninja
from binaryninja import PluginCommand, log_error, log_info

from . import sync

_links: dict = {}  # view -> Link
_followers: dict = {}  # view -> Follower


def _link(bv, quiet: bool = False):
    link = _links.get(bv)
    if link is None and not quiet:
        log_error("Fathom: not connected (Plugins > Fathom > Connect)")
    return link


def connect(bv) -> None:
    port = sync.DEFAULT_PORT
    try:
        link = sync.Link(port=port)
        base = link.attach_view(bv.file.original_filename or bv.file.filename, bv.start)
    except Exception as error:
        log_error(f"Fathom: cannot connect to pybridge on 127.0.0.1:{port} ({error}); run pybridge.start in Fathom")
        return
    _links[bv] = link
    log_info(f"Fathom: {link.module} is at {base:#x} in the debuggee (view {bv.start:#x}, delta {link.delta:#x})")


def follow(bv) -> None:
    link = _link(bv)
    if not link:
        return
    follower = _followers.pop(bv, None)
    if follower:
        follower.stop()
        log_info("Fathom: the view no longer follows the debugger")
        return

    def navigate(address: int) -> None:
        # from the polling thread: navigate on the main thread
        binaryninja.mainthread.execute_on_main_thread(lambda: bv.navigate(bv.file.view, address))

    follower = sync.Follower(link, bv.start, bv.end - bv.start, navigate)
    follower.start()
    _followers[bv] = follower
    log_info("Fathom: the view follows the instruction pointer of the debuggee")


def show(bv, address: int) -> None:
    link = _link(bv)
    if link:
        link.goto(address)


def set_breakpoint(bv, address: int) -> None:
    link = _link(bv)
    if link:
        link.breakpoint(address, True)
        log_info(f"Fathom: breakpoint at {link.to_debuggee(address):#x}")


def delete_breakpoint(bv, address: int) -> None:
    link = _link(bv)
    if link:
        link.breakpoint(address, False)


def run_to(bv, address: int) -> None:
    link = _link(bv)
    if link:
        link.run_to(address)


def import_trace(bv) -> None:
    path = binaryninja.interaction.get_open_filename_input("Trace exported by Fathom (TraceExport, json)", "*.json")
    if not path:
        return
    counts = sync.read_trace(path)
    link = _link(bv, quiet=True)
    delta = link.delta if link else 0
    if bv.get_tag_type("Fathom trace") is None:
        bv.create_tag_type("Fathom trace", "⏱")
    highlighted = 0
    for address, count in counts.items():
        view = address - delta
        for function in bv.get_functions_containing(view):
            function.set_user_instr_highlight(view, binaryninja.HighlightStandardColor.GreenHighlightColor)
            function.add_tag("Fathom trace", f"executed {count} times", view)
            highlighted += 1
    log_info(f"Fathom: {len(counts)} instructions in the trace, {highlighted} highlighted")


PluginCommand.register("Fathom\\Connect", "Connect to Fathom (pybridge) and map this view to the debuggee", connect)
PluginCommand.register("Fathom\\Follow the debugger", "The view follows the debuggee's instruction pointer (toggle)", follow)
PluginCommand.register_for_address("Fathom\\Show in Fathom", "Show this address in Fathom's disassembly", show)
PluginCommand.register_for_address("Fathom\\Set breakpoint", "Set a breakpoint here in Fathom", set_breakpoint)
PluginCommand.register_for_address("Fathom\\Delete breakpoint", "Delete the breakpoint here in Fathom", delete_breakpoint)
PluginCommand.register_for_address("Fathom\\Run to here", "Run the debuggee to this address", run_to)
PluginCommand.register("Fathom\\Import a trace", "Highlight the instructions of a trace exported by Fathom", import_trace)
