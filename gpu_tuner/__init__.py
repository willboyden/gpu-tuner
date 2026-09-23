"""gpu-tuner — a GUI for the power caps, fan curves and clock caps of the lab's two RTX PRO 6000s.

Two processes, split by privilege:
  daemon.py  root. Owns every NVML write and the fan loop. Validates each request against
             safety.py itself; it never trusts the UI.
  server.py  the logged-in user. Reads NVML (reads need no root), keeps the chart history,
             serves the page on 127.0.0.1 and forwards change requests to the daemon's socket.
"""

__version__ = "1.0.0"
