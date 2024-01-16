from __future__ import annotations

import argparse
import hashlib
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence, cast

from pdm import termui
from pdm.cli.commands.base import BaseCommand
from pdm.cli.options import lockfile_option
from pdm.exceptions import PdmUsageError
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

if TYPE_CHECKING:
    from typing import TypedDict

    from pdm.models.candidates import Candidate
    from pdm.project import Project

    class FileHash(TypedDict):
        url: str
        hash: str
        file: str


def _download_package(project: Project, package: FileHash, dest: Path) -> None:
    from unearth import Link

    hash_name, hash_value = package["hash"].split(":")
    hasher = hashlib.new(hash_name)
    with project.environment.get_finder() as finder:
        session = finder.session
        with session.get(package["url"], stream=True) as resp, dest.joinpath(
            package.get("file", Link(package["url"]).filename)
        ).open("wb") as fp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=8192):
                hasher.update(chunk)
                fp.write(chunk)
    if hasher.hexdigest() != hash_value:
        raise RuntimeError(
            f"Hash value of {package['file']} doesn't match. "
            f"Expected: {hash_value}, got: {hasher.hexdigest()}"
        )


def _download_packages(
    project: Project, packages: Sequence[FileHash], dest: Path
) -> None:
    if not dest.exists():
        dest.mkdir(parents=True)

    with Progress(
        TextColumn("[bold success]{task.description}"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.0f}%",
        "•",
        MofNCompleteColumn(),
        "•",
        TimeRemainingColumn(),
        "•",
        TaskProgressColumn(),
        transient=True,
        console=termui._console,
    ) as progress:
        task = progress.add_task("Downloading", total=len(packages))
        success_count = 0

        def progress_callback(future: Future) -> None:
            nonlocal success_count
            if future.exception():
                project.core.ui.echo(f"[error]Error: {future.exception()}", err=True)
            else:
                success_count += 1
            progress.update(task, advance=1)

        with ThreadPoolExecutor() as pool:
            for package in packages:
                future = pool.submit(_download_package, project, package, dest)
                future.add_done_callback(progress_callback)

        project.core.ui.echo(f"[success]{success_count} packages downloaded to {dest}.")


class Download(BaseCommand):
    """Download all packages from a lockfile for offline use"""

    arguments = [lockfile_option, *BaseCommand.arguments]

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-d",
            "--dest",
            help="The destination directory, default to './packages'",
            default="./packages",
            type=Path,
        )

    def handle(self, project: Project, options: argparse.Namespace) -> None:
        if not project.lockfile.exists():
            raise PdmUsageError(
                f"The lockfile '{options.lockfile or 'pdm.lock'}' doesn't exist."
            )
        locked_repository = project.locked_repository
        all_candidates = locked_repository.all_candidates.values()
        if "static_urls" in project.lockfile.strategy:
            hashes = cast(
                "list[FileHash]",
                [
                    hash_item
                    for candidate in all_candidates
                    for hash_item in locked_repository.get_hashes(candidate)
                ],
            )
        else:
            hashes = _get_file_hashes(project, all_candidates)
        _download_packages(project, hashes, options.dest)


def _convert_hash_option(hashes: list[FileHash]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item in hashes:
        hash_name, hash_value = item["hash"].split(":")
        result[hash_name].append(hash_value)
    return result


def _get_file_hashes(
    project: Project, candidates: Iterable[Candidate]
) -> list[FileHash]:
    hashes: list[FileHash] = []
    repository = project.get_repository()
    for candidate in candidates:
        can_hashes = candidate.hashes[:]
        if not can_hashes or not candidate.req.is_named:
            continue
        req = candidate.req.as_pinned_version(candidate.version)
        respect_source_order = project.environment.project.pyproject.settings.get(
            "resolution", {}
        ).get("respect-source-order", False)
        sources = repository.get_filtered_sources(candidate.req)
        comes_from = candidate.link.comes_from if candidate.link else None
        if req.is_named and respect_source_order and comes_from:
            sources = [s for s in sources if comes_from.startswith(s.url)]
        with project.environment.get_finder(
            sources, ignore_compatibility=True
        ) as finder:
            for package in finder.find_matches(
                req.as_line(),
                allow_yanked=True,
                allow_prereleases=True,
                hashes=_convert_hash_option(can_hashes),
            ):
                filename = package.link.filename
                match_hash = next(
                    (h for h in can_hashes if h["file"] == filename), None
                )
                if match_hash:
                    can_hashes.remove(match_hash)
                    hashes.append(
                        {
                            "url": package.link.url_without_fragment,
                            "file": filename,
                            "hash": match_hash["hash"],
                        }
                    )

            for item in can_hashes:
                project.core.ui.echo(
                    f"[warning]File {item['file']} not found on the repository.",
                    err=True,
                )
    return hashes
