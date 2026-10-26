"""Reproduce the bundled terminal assets using pinned upstream SHA-512 digests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "src" / "privy" / "hub" / "assets"
    packages = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    for package in packages:
        with urllib.request.urlopen(package["url"], timeout=30) as response:
            archive = response.read(4 * 1024 * 1024)
        actual = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode("ascii")
        if actual != package["integrity"]:
            raise ValueError(f"integrity mismatch for {package['name']} {package['version']}")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for member, name in package["files"].items():
                stream = source.extractfile(member)
                if stream is None:
                    raise ValueError(f"missing asset {member}")
                text = stream.read().decode("utf-8")
                if name.endswith(".js"):
                    text = re.sub(r"(?m)^//# sourceMappingURL=.*$", "", text)
                    if name == "xterm.js":
                        original = 'e.setAttribute("style",`${e.getAttribute("style")||""}${t};`)'
                        if text.count(original) != 1:
                            raise ValueError("pinned xterm CSP compatibility point changed")
                        text = text.replace(
                            original,
                            'e.style.setProperty(t.slice(0,t.indexOf(":")),t.slice(t.indexOf(":")+1))',
                        )
                    if "</script" in text.lower():
                        raise ValueError(f"asset cannot be embedded safely: {name}")
                with (destination / name).open("w", encoding="utf-8", newline="\n") as output:
                    output.write(text)
        print(f"Verified {package['name']} {package['version']}")


if __name__ == "__main__":
    main()
