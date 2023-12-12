from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdm.core import Core


def main(core: Core) -> None:
    from .command import Download

    core.register_command(Download, "download")
