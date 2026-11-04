// "Time Travel" tab for x64dbg (optional: built only when Qt5 is found, see CMakeLists.txt).
//
// Loads a Fathom Time Travel trace (.ftt, or a .trace32/.trace64 converted on the fly) and
// navigates it forward and backward without leaving the debugger: per-thread timelines with the
// context switches, the instructions around the position, the viewed thread's registers (changes
// highlighted) and the memory rebuilt at that instant. The navigation state is TtModel's, shared
// with the tt.* commands, so a script and the tab move together.
#pragma once

#ifdef EXTRACMDS_GUI

class QWidget;

// Build the tab widget (parented to nothing: ownership is handed to x64dbg by GuiAddQWidgetTab)
QWidget* CreateTimeTravelPanel();

#endif // EXTRACMDS_GUI
