"""Validate CA icon packaging; --public also checks anonymous GitHub downloads."""

import argparse
from pathlib import Path
import struct
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://raw.githubusercontent.com/0sy/gpu-tuner-unraid/main/"
ICON_URL = BASE + "icon.png"
MAX_BYTES = 2 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_png(data):
    require(data[:8] == b"\x89PNG\r\n\x1a\n", "Icon must contain PNG bytes, not SVG or an HTML error page")
    offset = 8
    kinds = []
    while offset < len(data):
        require(offset + 12 <= len(data), "Truncated PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + size
        require(end <= len(data), "Truncated PNG data")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        require(zlib.crc32(kind + payload) & 0xffffffff == crc, "Invalid PNG checksum")
        if not kinds:
            require(kind == b"IHDR" and size == 13, "Missing PNG header")
            require(struct.unpack(">II", payload[:8]) == (256, 256), "Icon must be 256 x 256")
        kinds.append(kind)
        offset = end
        if kind == b"IEND":
            require(size == 0 and offset == len(data), "Invalid PNG end")
            break
    require(kinds and kinds[-1] == b"IEND" and b"IDAT" in kinds, "Incomplete PNG")


def check_xml(data, filename):
    root = ET.fromstring(data)
    require(root.findtext("Icon") == ICON_URL, f"{filename}: Icon must point to the public icon.png")
    if root.tag == "Container":
        for target in ("GPU_TUNER_PUBLIC_URL", "GPU_TUNER_INITIAL_PASSWORD"):
            entries = [entry for entry in root.findall("Config") if entry.get("Target") == target]
            require(len(entries) == 1, f"{filename}: missing or duplicate {target}")
            entry = entries[0]
            require(not (entry.text or "").strip() and not entry.get("Default", "").strip(),
                    f"{filename}: {target} must ship with blank Value and Default")
            require(entry.get("Required") == "true", f"{filename}: {target} must be required")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "gpu-tuner-release-check"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(MAX_BYTES + 1)
        require(len(data) <= MAX_BYTES, "Public asset exceeds size limit")
        return data, response.headers.get_content_type()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", action="store_true", help="Require public URLs to work before publishing the image")
    args = parser.parse_args()
    icon = (ROOT / "icon.png").read_bytes()
    check_png(icon)
    for filename in ("templates/gpu-tuner.xml", "ca_profile.xml"):
        check_xml((ROOT / filename).read_bytes(), filename)
    print("PASS: valid PNG references; release WebUI URL and initial password are blank and required")
    if args.public:
        public, content_type = fetch(ICON_URL)
        require(content_type == "image/png", "Public icon must be served as image/png")
        check_png(public)
        require(public == icon, "Public icon differs from the release checkout; push icon.png first")
        for filename in ("templates/gpu-tuner.xml", "ca_profile.xml"):
            data, _ = fetch(BASE + filename)
            check_xml(data, filename)
        print("PASS: anonymous public icon/template/profile downloads use the validated PNG")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ET.ParseError) as error:
        raise SystemExit(f"FAIL: {error}")
