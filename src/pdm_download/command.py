from __future__ import annotations

import argparse
import hashlib
import re
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Sequence, cast

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
    from typing import ContextManager, TypedDict

    from httpx import Client, Response
    from pdm.models.candidates import Candidate
    from pdm.models.markers import EnvSpec
    from pdm.project import Project

    class FileHash(TypedDict):
        url: str
        hash: str
        file: str


def _iter_content_compat(resp: Any, chunk_size: int) -> Iterator[bytes]:
    if hasattr(resp, "iter_content"):
        return resp.iter_content(chunk_size)
    return resp.iter_bytes(chunk_size)


def _stream_compat(
    session: Client, url: str, **kwargs: Any
) -> ContextManager[Response]:
    if hasattr(session, "stream"):
        return session.stream("GET", url, **kwargs)
    return session.get(url, stream=True, **kwargs)


def _download_package(project: Project, package: FileHash, dest: Path) -> None:
    from unearth import Link

    hash_name, hash_value = package["hash"].split(":")
    hasher = hashlib.new(hash_name)
    with project.environment.get_finder() as finder:
        session = finder.session
        with _stream_compat(session, package["url"]) as resp, dest.joinpath(
            package.get("file", Link(package["url"]).filename)
        ).open("wb") as fp:
            resp.raise_for_status()
            for chunk in _iter_content_compat(resp, chunk_size=8192):
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
        parser.add_argument(
            "--python",
            help="Download packages for the given Python range. E.g. '>=3.9'",
        )
        parser.add_argument(
            "--platform", help="Download packages for the given platform. E.g. 'linux'"
        )
        parser.add_argument(
            "--implementation",
            help="Download packages for the given implementation. E.g. 'cpython', 'pypy'",
        )

    @staticmethod
    def _check_lock_targets(project: Project, env_spec: EnvSpec) -> None:
        from dep_logic.tags import EnvCompatibility
        from pdm.exceptions import PdmException

        lock_targets = project.get_locked_repository().targets
        ui = project.core.ui
        if env_spec in lock_targets:
            return
        compatibilities = [target.compare(env_spec) for target in lock_targets]
        if any(compat == EnvCompatibility.LOWER_OR_EQUAL for compat in compatibilities):
            return
        loose_compatible_target = next(
            (
                target
                for (target, compat) in zip(lock_targets, compatibilities)
                if compat == EnvCompatibility.HIGHER
            ),
            None,
        )
        if loose_compatible_target is not None:
            ui.warn(
                f"Found lock target {loose_compatible_target}, installing for env {env_spec}"
            )
        else:
            errors = [
                f"None of the lock targets matches the current env {env_spec}:"
            ] + [f" - {target}" for target in lock_targets]
            ui.error("\n".join(errors))
            raise PdmException("No compatible lock target found")

    def handle(self, project: Project, options: argparse.Namespace) -> None:
        from itertools import chain

        from pdm.models.specifiers import PySpecSet

        env_spec = project.environment.allow_all_spec

        if any([options.python, options.platform, options.implementation]):
            replace_dict = {}
            if options.python:
                if re.match(r"[\d.]+", options.python):
                    options.python = f">={options.python}"
                replace_dict["requires_python"] = PySpecSet(options.python)
            if options.platform:
                replace_dict["platform"] = options.platform
            if options.implementation:
                replace_dict["implementation"] = options.implementation
            env_spec = env_spec.replace(**replace_dict)

        if not project.lockfile.exists():
            raise PdmUsageError(
                f"The lockfile '{options.lockfile or 'pdm.lock'}' doesn't exist."
            )
        self._check_lock_targets(project, env_spec)
        locked_repository = project.get_locked_repository()
        all_candidates = chain.from_iterable(locked_repository.all_candidates.values())
        all_candidates = [
            c
            for c in all_candidates
            if c.req.marker is None or c.req.marker.matches(env_spec)
        ]
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
            hashes = _get_file_hashes(project, all_candidates, env_spec)
        _download_packages(project, hashes, options.dest)


def _convert_hash_option(hashes: list[FileHash]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item in hashes:
        hash_name, hash_value = item["hash"].split(":")
        result[hash_name].append(hash_value)
    return result


def _get_file_hashes(
    project: Project, candidates: Iterable[Candidate], env_spec: EnvSpec
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
        with project.environment.get_finder(sources, env_spec=env_spec) as finder:
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
