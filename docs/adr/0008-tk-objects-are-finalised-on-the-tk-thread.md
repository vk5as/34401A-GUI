# Tk Objects Are Finalised on the Tk Thread; the GUI Runs With Automatic GC Off

Python's cyclic garbage collector runs on whichever thread happens to allocate memory. The Worker thread (ADR-0002) allocates constantly, so it regularly triggers a collection. If that collection frees a Tk object (a `Variable`, a font, a widget, a matplotlib canvas) that sat in a reference cycle, Tcl is asked to finalise it on the wrong thread and the process aborts with `Fatal Python error: Aborted`, or segfaults. This showed up as roughly one aborted run in four of the full test suite as soon as the GUI had several tabs, and would hit users whenever a dialog or tab is created and destroyed.

So the rule is: **Tk objects are only ever finalised on the Tk thread.** Telling every tab and dialog author to avoid reference cycles does not hold up, so the policy is enforced centrally:

- `agilent34401a-gui` turns automatic garbage collection off for the life of the window, and `MainWindow` runs `gc.collect()` on the Tk thread every few seconds from its event-polling timer. Reference-counted garbage is still freed immediately; only cycles wait for the next sweep.
- The test suite does the same around GUI tests: collection is held off while a test has a window and runs on the main thread afterwards.

## Considered Options

- **Break every cycle by hand on destroy:** rejected as the only defence, because one forgotten cycle in a future tab brings the crash back. Avoiding cycles is still good practice.
- **`gc.freeze()` or per-generation tuning:** rejected, because it only lowers the odds of a collection on the Worker thread rather than ruling it out.
- **Run the GUI's Tk work on the Worker thread:** rejected, because it gives up the whole point of ADR-0002.
