"""DisplayDesktop launcher. Works from any folder and with any Python 3.10+:

    pythonw DisplayDesktop.pyw          run the app (this is what double-clicking it does)
    python  DisplayDesktop.pyw status   a command (start, stop, restart, status, enable-startup,
                                       disable-startup); run.cmd does this for you
"""
import os
import sys

sys.dont_write_bytecode = True  # leave no __pycache__ folders behind
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.version_info < (3, 10):
    message = "DisplayDesktop needs Python 3.10 or newer (this is %d.%d)." % sys.version_info[:2]
    if sys.stdout is not None:
        print(message)
    else:  # pythonw: no console to print to
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "DisplayDesktop", 0x10)
    sys.exit(1)

from src.app import main  # noqa: E402

sys.exit(main())
