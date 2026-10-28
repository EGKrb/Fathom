// HvKdFusion GUI panel for x64dbg (optional: built only when Qt5 is found, see CMakeLists.txt).
//
// A single dockable "HvKdFusion" tab that drives the HvKdFusion Python tools through their --json
// output: a Hyper-V VM selector, a component-readiness table, and a process explorer (processes ->
// modules / VADs / handles) built on wineye. Dump / Debug / Mount launch the matching hvkd action.
//
// The panel is decoupled from the command plugin: everything it needs is passed in HvKdContext, so
// extracmds.cpp stays the single owner of the paths and of the elevated launcher (hvkd.ps1).
#pragma once

#ifdef EXTRACMDS_GUI

#include <functional>
#include <string>

class QWidget;

struct HvKdContext
{
    std::wstring python; // python.exe / py.exe on the PATH (empty if none was found)
    std::wstring folder; // HvKdFusion directory, the one that holds hvkdfusion.py
    // Run "hvkd.ps1 <arguments>" in a new console (elevation prompt when the action needs it); this
    // reuses the plugin's single launcher so the GUI never duplicates the process-creation logic.
    std::function<void(const std::wstring & arguments)> launch;
};

// Build the panel widget (parented to nothing: ownership is handed to x64dbg by GuiAddQWidgetTab).
// Returns nullptr when the context is unusable (no HvKdFusion folder).
QWidget* CreateHvKdPanel(const HvKdContext & context);

#endif // EXTRACMDS_GUI
