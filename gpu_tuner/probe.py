#!/usr/bin/env python3
"""gpu-tuner probe — a READ-ONLY report of what NVML exposes on this machine, as JSON.

Self-contained (stdlib + pynvml only) so it can run on a machine gpu-tuner isn't installed on:

    ssh <host> python3 - < gpu_tuner/probe.py

Every getter gpu-tuner uses is called once per GPU and recorded as {"ok": value} or
{"err": "<NVML error>"}. Nothing is written: there is no NVML "can I set this?" query, so whether a
setter actually works on a card is only proven by the daemon trying it. Process lists are counted,
never listed (no PIDs, no command lines).
"""
import ctypes
import json
import platform
import sys

SETTERS = ("nvmlDeviceSetPowerManagementLimit", "nvmlDeviceSetFanSpeed_v2",
           "nvmlDeviceSetDefaultFanSpeed_v2", "nvmlDeviceSetGpuLockedClocks",
           "nvmlDeviceResetGpuLockedClocks", "nvmlDeviceSetPersistenceMode")


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


def _plain(v):
    """ctypes structs and lists -> JSON-able values."""
    if isinstance(v, (bytes, str, int, float, bool)) or v is None:
        return _s(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    fields = getattr(v, "_fields_", None)
    if fields:
        return {name: _plain(getattr(v, name)) for name, *_ in fields}
    return repr(v)


def main():
    out = {"probe": 1, "arch": platform.machine(), "python": platform.python_version()}
    try:
        import pynvml as n
    except ImportError as e:
        out["error"] = f"pynvml not importable: {e} (apt install python3-pynvml)"
        print(json.dumps(out, indent=1))
        return 1
    try:
        from importlib import metadata
        out["pynvml"] = next((metadata.version(d) for d in ("nvidia-ml-py", "pynvml")
                              if _has_dist(metadata, d)), None)
    except ImportError:
        out["pynvml"] = None
    out["setters_present"] = {s: hasattr(n, s) for s in SETTERS}
    try:
        with open("/proc/meminfo") as f:
            out["meminfo_total_kib"] = int(next(l for l in f if l.startswith("MemTotal:")).split()[1])
    except (OSError, StopIteration, ValueError):
        out["meminfo_total_kib"] = None
    try:
        n.nvmlInit()
    except n.NVMLError as e:
        out["error"] = f"nvmlInit failed: {e}"
        print(json.dumps(out, indent=1))
        return 1

    def call(name, *args):
        fn = getattr(n, name, None)
        if fn is None:
            return {"err": "missing in this pynvml"}
        try:
            return {"ok": _plain(fn(*args))}
        except n.NVMLError as e:
            return {"err": str(e)}
        except Exception as e:      # noqa: BLE001 — report, never crash the probe
            return {"err": f"{type(e).__name__}: {e}"}

    out["driver"] = call("nvmlSystemGetDriverVersion")
    out["nvml_version"] = call("nvmlSystemGetNVMLVersion")
    count = call("nvmlDeviceGetCount")
    out["count"] = count
    gpus = []
    for i in range(count.get("ok") or 0):
        h = n.nvmlDeviceGetHandleByIndex(i)
        g = {"index": i}
        for key, name, args in (
                ("name", "nvmlDeviceGetName", ()),
                ("uuid", "nvmlDeviceGetUUID", ()),
                ("cuda_cc", "nvmlDeviceGetCudaComputeCapability", ()),
                ("pci", "nvmlDeviceGetPciInfo", ()),
                ("vbios", "nvmlDeviceGetVbiosVersion", ()),
                ("num_fans", "nvmlDeviceGetNumFans", ()),
                ("fan0_speed", "nvmlDeviceGetFanSpeed_v2", (0,)),
                ("fan0_target", "nvmlDeviceGetTargetFanSpeed", (0,)),
                ("power_constraints_mw", "nvmlDeviceGetPowerManagementLimitConstraints", ()),
                ("power_default_mw", "nvmlDeviceGetPowerManagementDefaultLimit", ()),
                ("power_limit_mw", "nvmlDeviceGetPowerManagementLimit", ()),
                ("power_usage_mw", "nvmlDeviceGetPowerUsage", ()),
                ("energy_mj", "nvmlDeviceGetTotalEnergyConsumption", ()),
                ("temp_gpu", "nvmlDeviceGetTemperature", (getattr(n, "NVML_TEMPERATURE_GPU", 0),)),
                ("supported_mem_clocks", "nvmlDeviceGetSupportedMemoryClocks", ()),
                ("clock_graphics", "nvmlDeviceGetClockInfo", (getattr(n, "NVML_CLOCK_GRAPHICS", 0),)),
                ("clock_mem", "nvmlDeviceGetClockInfo", (getattr(n, "NVML_CLOCK_MEM", 2),)),
                ("max_clock_graphics", "nvmlDeviceGetMaxClockInfo", (getattr(n, "NVML_CLOCK_GRAPHICS", 0),)),
                ("utilization", "nvmlDeviceGetUtilizationRates", ()),
                ("memory_info", "nvmlDeviceGetMemoryInfo", ()),
                ("pstate", "nvmlDeviceGetPerformanceState", ()),
                ("clocks_event_reasons", "nvmlDeviceGetCurrentClocksEventReasons", ()),
                ("clocks_throttle_reasons", "nvmlDeviceGetCurrentClocksThrottleReasons", ()),
                ("persistence", "nvmlDeviceGetPersistenceMode", ()),
                ("encoder", "nvmlDeviceGetEncoderUtilization", ()),
                ("decoder", "nvmlDeviceGetDecoderUtilization", ()),
                ("ecc_mode", "nvmlDeviceGetEccMode", ()),
                ("pcie_gen", "nvmlDeviceGetCurrPcieLinkGeneration", ()),
                ("pcie_width", "nvmlDeviceGetCurrPcieLinkWidth", ())):
            g[key] = call(name, h, *args)
        mem = g["supported_mem_clocks"].get("ok")
        if mem:
            clocks = call("nvmlDeviceGetSupportedGraphicsClocks", h, max(mem))
            if "ok" in clocks:
                lst = clocks["ok"] or []
                clocks = {"ok": {"count": len(lst), "min": min(lst) if lst else None,
                                 "max": max(lst) if lst else None}}
            g["supported_graphics_clocks"] = clocks
        lo, hi = ctypes.c_uint(), ctypes.c_uint()
        fn = getattr(n, "nvmlDeviceGetMinMaxFanSpeed", None)
        if fn is None:
            g["fan_min_max"] = {"err": "missing in this pynvml"}
        else:
            try:
                fn(h, ctypes.byref(lo), ctypes.byref(hi))
                g["fan_min_max"] = {"ok": [lo.value, hi.value]}
            except (n.NVMLError, TypeError) as e:
                g["fan_min_max"] = {"err": str(e)}
        for key, const in (("t_limit", "NVML_TEMPERATURE_THRESHOLD_GPU_MAX"),
                           ("slowdown", "NVML_TEMPERATURE_THRESHOLD_SLOWDOWN"),
                           ("shutdown", "NVML_TEMPERATURE_THRESHOLD_SHUTDOWN")):
            c = getattr(n, const, None)
            g["threshold_" + key] = (call("nvmlDeviceGetTemperatureThreshold", h, c) if c is not None
                                     else {"err": "constant missing in this pynvml"})
        for key, name in (("compute_procs", "nvmlDeviceGetComputeRunningProcesses"),
                          ("graphics_procs", "nvmlDeviceGetGraphicsRunningProcesses")):
            r = call(name, h)
            g[key] = {"ok": len(r["ok"])} if "ok" in r else r
        gpus.append(g)
    out["gpus"] = gpus
    try:
        n.nvmlShutdown()
    except n.NVMLError:
        pass
    print(json.dumps(out, indent=1))
    return 0


def _has_dist(metadata, name):
    try:
        metadata.version(name)
        return True
    except metadata.PackageNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(main())
