"""Locate shipped read-only resources in a checkout or an installed wheel.

Wheels are unpacked by Python installers. No resources are extracted into user
storage and mutable project/cache paths remain controlled by configuration.
"""

from pathlib import Path


def resource_path(*parts: str) -> Path:
    package = Path(__file__).resolve().parents[1]
    bundled = package / "_resources"
    root = bundled if bundled.is_dir() else package.parent
    return root.joinpath(*parts)
