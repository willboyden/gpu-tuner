"""Thin NVML wrapper shared by the daemon (writes, root) and the UI server (reads, no root).

The NVML module is injected so tests can pass a fake; nothing here needs a GPU to be tested.
With dry_run=True every write is recorded and skipped, and reads of the things we "wrote" come
back from that record, so the whole control path can be exercised as a normal user.
"""
from __future__ import annotations

import ctypes

# nvmlClocksEventReasons bits. Spelled out because the installed nvidia-ml-py (12.550) predates
# the driver (595) and names some of these differently.
REASONS = (
    (0x0001, "idle", "Idle"),
    (0x0002, "app_clocks", "Application clock setting"),
    (0x0004, "sw_power_cap", "At power cap"),
    (0x0008, "hw_slowdown", "Hardware slowdown"),
    (0x0010, "sync_boost", "Sync boost"),
    (0x0020, "sw_thermal", "Software thermal slowdown"),
    (0x0040, "hw_thermal", "Hardware thermal slowdown"),
    (0x0080, "hw_power_brake", "Hardware power brake"),
    (0x0100, "display_clock", "Display clock setting"),
)


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


class Gpu:
    def __init__(self, index, handle):
        self.index, self.handle = index, handle
        self.uuid = self.name = self.pci = ""
        self.nfans = 0
        self.fan_min, self.fan_max = 30, 100
        self.power_min_w = self.power_max_w = self.power_default_w = 0
        self.thresholds = {}
        self.clocks = []          # supported graphics clocks (MHz) at the top memory clock
        self.vbios = ""

    def static(self):
        return {"index": self.index, "uuid": self.uuid, "name": self.name, "pci": self.pci,
                "vbios": self.vbios, "nfans": self.nfans, "fan_min": self.fan_min,
                "fan_max": self.fan_max, "power_min_w": self.power_min_w,
                "power_max_w": self.power_max_w, "power_default_w": self.power_default_w,
                "thresholds": self.thresholds,
                "clock_min_mhz": min(self.clocks) if self.clocks else None,
                "clock_max_mhz": max(self.clocks) if self.clocks else None}


class Nvml:
    def __init__(self, lib=None, dry_run=False):
        if lib is None:
            import pynvml as lib      # nvidia-ml-py; system-wide here, so root sees it too
        self.n, self.dry_run = lib, dry_run
        self.Error = lib.NVMLError
        self.writes = []              # audit trail of every write attempted (dry-run included)
        self._shadow = {}             # (uuid, what) -> value, dry-run only
        lib.nvmlInit()
        self.driver = _s(self._try(lib.nvmlSystemGetDriverVersion) or "")
        self.gpus = [self._load(i) for i in range(lib.nvmlDeviceGetCount())]

    def close(self):
        try:
            self.n.nvmlShutdown()
        except self.Error:
            pass

    def _try(self, fn, *a):
        try:
            return fn(*a)
        except self.Error:
            return None

    def _load(self, i):
        n = self.n
        g = Gpu(i, n.nvmlDeviceGetHandleByIndex(i))
        h = g.handle
        g.uuid, g.name = _s(n.nvmlDeviceGetUUID(h)), _s(n.nvmlDeviceGetName(h))
        pci = self._try(n.nvmlDeviceGetPciInfo, h)
        g.pci = _s(pci.busId) if pci is not None else ""
        g.vbios = _s(self._try(n.nvmlDeviceGetVbiosVersion, h) or "")
        g.nfans = self._try(n.nvmlDeviceGetNumFans, h) or 0
        lo, hi = ctypes.c_uint(), ctypes.c_uint()
        try:
            n.nvmlDeviceGetMinMaxFanSpeed(h, ctypes.byref(lo), ctypes.byref(hi))
            g.fan_min, g.fan_max = int(lo.value), int(hi.value)
        except (self.Error, TypeError):
            pass
        con = self._try(n.nvmlDeviceGetPowerManagementLimitConstraints, h)
        if con:
            g.power_min_w, g.power_max_w = con[0] // 1000, con[1] // 1000
        g.power_default_w = (self._try(n.nvmlDeviceGetPowerManagementDefaultLimit, h) or 0) // 1000
        for key, const in (("t_limit", "NVML_TEMPERATURE_THRESHOLD_GPU_MAX"),
                           ("slowdown", "NVML_TEMPERATURE_THRESHOLD_SLOWDOWN"),
                           ("shutdown", "NVML_TEMPERATURE_THRESHOLD_SHUTDOWN")):
            c = getattr(n, const, None)
            v = self._try(n.nvmlDeviceGetTemperatureThreshold, h, c) if c is not None else None
            if v:
                g.thresholds[key] = int(v)
        mem = self._try(n.nvmlDeviceGetSupportedMemoryClocks, h)
        if mem:
            g.clocks = sorted(self._try(n.nvmlDeviceGetSupportedGraphicsClocks, h, max(mem)) or [])
        return g

    def by_uuid(self, uuid):
        return next((g for g in self.gpus if g.uuid == uuid), None)

    # ── reads ──────────────────────────────────────────────────────────────────────────────
    def temp_c(self, g):
        """Raises on failure: the fan loop must KNOW it has no reading, not get a default."""
        return int(self.n.nvmlDeviceGetTemperature(g.handle, self.n.NVML_TEMPERATURE_GPU))

    def power_limit_w(self, g):
        if self.dry_run and (g.uuid, "power") in self._shadow:
            return self._shadow[(g.uuid, "power")]
        v = self._try(self.n.nvmlDeviceGetPowerManagementLimit, g.handle)
        return None if v is None else int(round(v / 1000))

    def sample(self, g):
        n, h, t = self.n, g.handle, self._try
        util = t(n.nvmlDeviceGetUtilizationRates, h)
        mem = t(n.nvmlDeviceGetMemoryInfo, h)
        draw = t(n.nvmlDeviceGetPowerUsage, h)
        reasons_fn = getattr(n, "nvmlDeviceGetCurrentClocksEventReasons", None) or \
            getattr(n, "nvmlDeviceGetCurrentClocksThrottleReasons")
        mask = t(reasons_fn, h)
        persistence = t(n.nvmlDeviceGetPersistenceMode, h)
        enc = t(n.nvmlDeviceGetEncoderUtilization, h)
        dec = t(n.nvmlDeviceGetDecoderUtilization, h)
        ecc = t(n.nvmlDeviceGetEccMode, h)
        ecc_on = None if ecc is None else bool(ecc[0])
        corrected = uncorrected = None
        if ecc_on:
            # NVML_VOLATILE_ECC, not …_ECC_ERRORS: the installed nvidia-ml-py names this
            # differently from what NVML's own header/docs suggest (same vintage mismatch as the
            # REASONS bits above).
            corrected = t(n.nvmlDeviceGetTotalEccErrors, h, n.NVML_MEMORY_ERROR_TYPE_CORRECTED,
                          n.NVML_VOLATILE_ECC)
            uncorrected = t(n.nvmlDeviceGetTotalEccErrors, h, n.NVML_MEMORY_ERROR_TYPE_UNCORRECTED,
                            n.NVML_VOLATILE_ECC)
        return {
            "temp": t(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU),
            "fans": [t(n.nvmlDeviceGetFanSpeed_v2, h, f) for f in range(g.nfans)],
            "fan_target": t(n.nvmlDeviceGetTargetFanSpeed, h, 0) if g.nfans else None,
            "power_w": None if draw is None else round(draw / 1000, 1),
            "power_limit_w": self.power_limit_w(g),
            "util": None if util is None else int(util.gpu),
            "mem_util": None if util is None else int(util.memory),
            "vram_used_mib": None if mem is None else int(mem.used) >> 20,
            "vram_total_mib": None if mem is None else int(mem.total) >> 20,
            "clock_mhz": t(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_GRAPHICS),
            "mem_clock_mhz": t(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM),
            "pstate": t(n.nvmlDeviceGetPerformanceState, h),
            "reasons": [] if mask is None else [k for bit, k, _ in REASONS if mask & bit],
            "pcie_gen": t(n.nvmlDeviceGetCurrPcieLinkGeneration, h),
            "pcie_gen_max": t(n.nvmlDeviceGetMaxPcieLinkGeneration, h),
            "pcie_width": t(n.nvmlDeviceGetCurrPcieLinkWidth, h),
            "pcie_width_max": t(n.nvmlDeviceGetMaxPcieLinkWidth, h),
            "energy_j": (lambda e: None if e is None else int(e) // 1000)(
                t(n.nvmlDeviceGetTotalEnergyConsumption, h)),
            "persistence": None if persistence is None else bool(persistence),
            "encoder_util": None if enc is None else int(enc[0]),
            "decoder_util": None if dec is None else int(dec[0]),
            "ecc_enabled": ecc_on,
            "ecc_corrected_total": corrected,
            "ecc_uncorrected_total": uncorrected,
        }

    def processes(self, g):
        out = []
        for getter in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
            for p in self._try(getattr(self.n, getter), g.handle) or []:
                used = getattr(p, "usedGpuMemory", None)
                out.append({"pid": int(p.pid), "vram_mib": None if not used else int(used) >> 20})
        return out

    # ── writes (root) ──────────────────────────────────────────────────────────────────────
    def _write(self, g, what, value, fn, *args):
        self.writes.append((g.uuid, what, value))
        if self.dry_run:
            self._shadow[(g.uuid, what)] = value
            return
        fn(*args)

    def set_persistence(self, g):
        self._write(g, "persistence", 1, self.n.nvmlDeviceSetPersistenceMode, g.handle, 1)

    def set_power_limit_w(self, g, watts):
        self._write(g, "power", watts, self.n.nvmlDeviceSetPowerManagementLimit, g.handle, watts * 1000)

    def set_fan_pct(self, g, pct):
        for f in range(g.nfans):
            self._write(g, f"fan{f}", pct, self.n.nvmlDeviceSetFanSpeed_v2, g.handle, f, pct)

    def set_fan_auto(self, g):
        """Hand every fan back to the driver's own curve. Tries ALL fans even if one fails."""
        err = None
        for f in range(g.nfans):
            try:
                self._write(g, f"fan{f}", "auto", self.n.nvmlDeviceSetDefaultFanSpeed_v2, g.handle, f)
            except self.Error as e:
                err = e
        if err is not None:
            raise err

    def set_clock_cap(self, g, max_mhz):
        lo = min(g.clocks) if g.clocks else 0
        self._write(g, "clock_cap", max_mhz, self.n.nvmlDeviceSetGpuLockedClocks, g.handle, lo, max_mhz)

    def reset_clock_cap(self, g):
        self._write(g, "clock_cap", None, self.n.nvmlDeviceResetGpuLockedClocks, g.handle)
