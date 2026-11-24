"""Bundled browser assets shared by interactive HTML exports."""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def vis_network_script() -> str:
    """Read the standalone vis-network 10.1.5 UMD bundle on first export."""
    vendor = files("memory.reporting").joinpath("vendor")
    license_text = vendor.joinpath("vis-network.LICENSE-MIT").read_text(encoding="utf-8")
    script = vendor.joinpath("vis-network.min.js").read_text(encoding="utf-8")
    return f"/*\n{license_text}*/\n{script}"
