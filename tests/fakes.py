"""Fake pynvml modules for the offline tests and the fake nodes. Nothing here needs a GPU.

  FakeLib     two RTX PRO 6000 cards, Max-Q + Workstation (the original test fake)
  FakeGB10    a DGX Spark-like SoC GPU: no power limit, no fans, no memory info, no clock list,
              persistence NOT_SUPPORTED — what NVIDIA's forum reports describe for GB10
  Fake5090    a GeForce RTX 5090: settable power (no tuned profile), two fans, clocks
  FakeOldLib  FakeLib through an older pynvml that lacks some bindings entirely
"""
import os

NOT_SUPPORTED = 3

MAXQ = "GPU-maxq-0000"
WS = "GPU-ws-0000"
GB10 = "GPU-gb10-0000"
RTX5090 = "GPU-5090-0000"


class FakeNVMLError(Exception):
    def __init__(self, value=None):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return {NOT_SUPPORTED: "Not Supported", 13: "Function Not Found"}.get(self.value, str(self.value))


class _Util:
    def __init__(self, gpu, memory):
        self.gpu, self.memory = gpu, memory


class _Mem:
    def __init__(self, used, total):
        self.used, self.total, self.free = used, total, total - used


class _Proc:
    def __init__(self, pid, used):
        self.pid, self.usedGpuMemory = pid, used


class FakeLib:
    """Just enough of pynvml for Nvml(), the daemon and the node."""
    NVMLError = FakeNVMLError
    NVML_ERROR_NOT_SUPPORTED = NOT_SUPPORTED
    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_GRAPHICS, NVML_CLOCK_MEM = 0, 2
    NVML_TEMPERATURE_THRESHOLD_SHUTDOWN, NVML_TEMPERATURE_THRESHOLD_SLOWDOWN, NVML_TEMPERATURE_THRESHOLD_GPU_MAX = 0, 1, 3

    def __init__(self):
        self.cards = [
            {"uuid": MAXQ, "name": "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition", "fans": 1,
             "limit": 300000, "con": [250000, 325000], "default": 300000, "temp": 40},
            {"uuid": WS, "name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "fans": 2,
             "limit": 450000, "con": [150000, 600000], "default": 600000, "temp": 35},
        ]
        self.fan_writes, self.fan_auto, self.refuse_fan, self.refuse_temp = [], [], False, False
        self.clock_calls, self.power_writes, self.persistence_calls = [], [], []

    def nvmlInit(self): pass
    def nvmlShutdown(self): pass
    def nvmlSystemGetDriverVersion(self): return "595.84"
    def nvmlDeviceGetCount(self): return len(self.cards)
    def nvmlDeviceGetHandleByIndex(self, i): return i
    def nvmlDeviceGetUUID(self, h): return self.cards[h]["uuid"]
    def nvmlDeviceGetName(self, h): return self.cards[h]["name"]
    def nvmlDeviceGetPciInfo(self, h): raise FakeNVMLError("n/a")
    def nvmlDeviceGetVbiosVersion(self, h): return "98.02"
    def nvmlDeviceGetNumFans(self, h): return self.cards[h]["fans"]
    def nvmlDeviceGetMinMaxFanSpeed(self, h, lo, hi): lo._obj.value, hi._obj.value = 30, 100   # byref() args
    def nvmlDeviceGetPowerManagementLimitConstraints(self, h): return self.cards[h]["con"]
    def nvmlDeviceGetPowerManagementDefaultLimit(self, h): return self.cards[h]["default"]
    def nvmlDeviceGetPowerManagementLimit(self, h): return self.cards[h]["limit"]
    def nvmlDeviceGetTemperatureThreshold(self, h, c): return {0: 98, 1: 95, 3: 92}[c]
    def nvmlDeviceGetSupportedMemoryClocks(self, h): return [14001, 405]
    def nvmlDeviceGetSupportedGraphicsClocks(self, h, m): return list(range(180, 3091, 15))
    def nvmlDeviceGetPowerUsage(self, h): return self.cards[h]["limit"] // 2
    def nvmlDeviceGetUtilizationRates(self, h): return _Util(50, 20)
    def nvmlDeviceGetMemoryInfo(self, h): return _Mem(10 << 30, 96 << 30)
    def nvmlDeviceGetClockInfo(self, h, which): return 1800 if which == 0 else 14001
    def nvmlDeviceGetFanSpeed_v2(self, h, f): return 45
    def nvmlDeviceGetComputeRunningProcesses(self, h): return [_Proc(1, 8 << 30)]

    def nvmlDeviceGetTemperature(self, h, _k):
        if self.refuse_temp:
            raise FakeNVMLError("Unknown Error")
        return self.cards[h]["temp"]

    def nvmlDeviceSetPersistenceMode(self, h, v): self.persistence_calls.append((h, v))
    def nvmlDeviceSetPowerManagementLimit(self, h, mw):
        self.power_writes.append((h, mw))
        self.cards[h]["limit"] = mw
    def nvmlDeviceSetFanSpeed_v2(self, h, f, pct):
        if self.refuse_fan:
            raise FakeNVMLError("Insufficient Permissions")
        self.fan_writes.append((h, f, pct))
    def nvmlDeviceSetDefaultFanSpeed_v2(self, h, f): self.fan_auto.append((h, f))
    def nvmlDeviceSetGpuLockedClocks(self, h, lo, hi): self.clock_calls.append((h, lo, hi))
    def nvmlDeviceResetGpuLockedClocks(self, h): self.clock_calls.append((h, None))


def _unsupported(*_a):
    raise FakeNVMLError(NOT_SUPPORTED)


class FakeGB10(FakeLib):
    """One integrated GB10: power, fans, memory info and clock lists all NOT_SUPPORTED."""

    def __init__(self, uuid=GB10):
        super().__init__()
        self.cards = [{"uuid": uuid, "name": "NVIDIA GB10", "fans": 0, "limit": None, "con": None,
                       "default": None, "temp": 52}]

    def nvmlSystemGetDriverVersion(self): return "580.95.05"
    nvmlDeviceGetNumFans = staticmethod(_unsupported)
    nvmlDeviceGetMinMaxFanSpeed = staticmethod(_unsupported)
    nvmlDeviceGetPowerManagementLimitConstraints = staticmethod(_unsupported)
    nvmlDeviceGetPowerManagementDefaultLimit = staticmethod(_unsupported)
    nvmlDeviceGetPowerManagementLimit = staticmethod(_unsupported)
    nvmlDeviceGetMemoryInfo = staticmethod(_unsupported)
    nvmlDeviceGetSupportedMemoryClocks = staticmethod(_unsupported)
    nvmlDeviceSetPersistenceMode = staticmethod(_unsupported)
    nvmlDeviceGetFanSpeed_v2 = staticmethod(_unsupported)
    def nvmlDeviceGetPowerUsage(self, h): return 31000
    # what four real GB10s reported (2026-10-03): T.Limit above slowdown and shutdown, and a
    # meaningless "Gen 1 x1 of x16" PCIe link for a GPU that has no slot
    def nvmlDeviceGetTemperatureThreshold(self, h, c): return {0: 90, 1: 86, 3: 99}[c]
    def nvmlDeviceGetCurrPcieLinkGeneration(self, h): return 1
    def nvmlDeviceGetMaxPcieLinkGeneration(self, h): return 1
    def nvmlDeviceGetCurrPcieLinkWidth(self, h): return 1
    def nvmlDeviceGetMaxPcieLinkWidth(self, h): return 16

    def nvmlDeviceSetPowerManagementLimit(self, h, mw):
        self.power_writes.append((h, mw))
        raise FakeNVMLError(NOT_SUPPORTED)


class Fake5090(FakeLib):
    def __init__(self, uuid=RTX5090):
        super().__init__()
        self.cards = [{"uuid": uuid, "name": "NVIDIA GeForce RTX 5090", "fans": 2, "limit": 575000,
                       "con": [400000, 600000], "default": 575000, "temp": 48}]

    def nvmlDeviceGetMemoryInfo(self, h): return _Mem(4 << 30, 32 << 30)


class FakeOldLib(FakeLib):
    """FakeLib as seen through an older pynvml: these names simply don't exist."""
    MISSING = {"nvmlDeviceGetFanSpeed_v2", "nvmlDeviceSetFanSpeed_v2", "nvmlDeviceSetDefaultFanSpeed_v2",
               "nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons",
               "NVML_VOLATILE_ECC", "nvmlDeviceGetMinMaxFanSpeed"}

    def __getattribute__(self, name):
        if name in FakeOldLib.MISSING:
            raise AttributeError(name)
        return super().__getattribute__(name)


KINDS = {"pair": FakeLib, "gb10": FakeGB10, "5090": Fake5090}


def meminfo_file(tmp, total_kib=128 * 1024 * 1024, avail_kib=96 * 1024 * 1024):
    path = os.path.join(tmp, "meminfo")
    with open(path, "w") as f:
        f.write(f"MemTotal:       {total_kib} kB\nMemFree:        1 kB\nMemAvailable:   {avail_kib} kB\n")
    return path
