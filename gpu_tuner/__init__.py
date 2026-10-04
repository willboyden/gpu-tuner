"""gpu-tuner — power caps, fan curves and clock caps for NVIDIA GPUs, on one machine or several.

Per machine, two processes split by privilege:
  daemon.py  root. Owns every NVML write and the fan loop. Validates each request against
             safety.py itself; it never trusts whoever sent it.
  node.py    the logged-in user. Reads NVML (reads need no root) and forwards change requests to
             that machine's daemon socket. Spoken to over stdin/stdout — locally, or over ssh.
And one page for all of them:
  server.py  the logged-in user, on the machine you sit at: serves the page on 127.0.0.1 and
  hub.py     talks to every machine's node (hosts.py says which, and how), keeping the history.
"""

__version__ = "1.1.1"
