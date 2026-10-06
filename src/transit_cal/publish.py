"""Promote a built, validated feed tree to the served release, and prune old releases."""

import os
import re
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

STAGING = "staging"
RELEASES = "releases"
CURRENT = "current"
# One writer at a time: bin/build-feeds holds a lock across the whole run.
NEXT = ".current-next"
RELEASE_NAME = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z")


class PublishError(ValueError):
    """The stage cannot be published; the served release is unchanged."""


def publish(stage: Path, feeds: Path, now: datetime) -> tuple[Path, Path | None]:
    """Move stage to feeds/releases/<UTC time> and point feeds/current at it.

    Returns the new release and the one served before, if any. stage must be a real directory
    directly under feeds/staging holding only directories and regular .ics files, at least one,
    and every feed path the served release has. The release is made read-only before current
    moves. Raises PublishError, with nothing changed, for a bad stage, a naive now, a
    feeds/current that is not a symlink, or a release name already taken. An OSError before the
    swap puts stage back and leaves current.
    """
    if now.tzinfo is None:
        raise PublishError("now must carry a timezone")
    if stage.is_symlink() or stage.parent.resolve() != (feeds / STAGING).resolve():
        raise PublishError(f"{stage}: a stage must be a directory in {feeds / STAGING}")
    if not stage.is_dir() or not (staged := _feed_paths(stage)):
        raise PublishError(f"{stage}: no .ics feeds to publish")
    current = feeds / CURRENT
    if current.exists() and not current.is_symlink():
        raise PublishError(f"{current} exists and is not a symlink")
    previous = (feeds / os.readlink(current)) if current.is_symlink() else None
    # A subscribed URL must never start returning 404, whatever a build left out
    if previous is not None and (dropped := sorted(_served_paths(previous) - staged)):
        raise PublishError(f"{stage}: would drop the published feed {dropped[0]}")
    releases = feeds / RELEASES
    release = releases / now.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    if release.exists():
        raise PublishError(f"{release} already exists")

    releases.mkdir(exist_ok=True)
    stage.rename(release)
    link = feeds / NEXT
    try:
        _set_modes(release, files=0o444, dirs=0o555)
        link.unlink(missing_ok=True)
        link.symlink_to(Path(RELEASES) / release.name)
        os.replace(link, current)
    except OSError:
        # Moving a directory needs write access to it, to update its ".." entry
        _set_modes(release, files=0o644, dirs=0o755)
        release.rename(stage)
        link.unlink(missing_ok=True)
        raise
    return release, previous


def prune(releases: Path, keep: int, spare: tuple[Path | None, ...]) -> list[Path]:
    """Delete all but the newest keep releases this module named; returns those deleted.

    Never deletes a spared release, symlinks, or names it did not create. Callers spare the
    served release and the one served before it, which readers may still be resolving.
    """
    if keep < 1:
        raise PublishError(f"keep must be at least 1, not {keep}")
    ours = sorted(p for p in releases.iterdir() if _ours(p))
    doomed = [p for p in ours[:-keep] if p not in spare]
    for old in doomed:
        _set_modes(old, files=0o644, dirs=0o755)
        shutil.rmtree(old)
    return doomed


def _feed_paths(stage: Path) -> set[Path]:
    """Every .ics file under stage, relative to it. Raises PublishError for anything else.

    Symlinks are never followed: Caddy would serve whatever a link inside a release points at.
    """
    found = set()
    for root, dirs, files in os.walk(stage, onerror=_raise):
        for name in dirs + files:
            path = Path(root, name)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode) or not name.endswith(".ics"):
                raise PublishError(f"{path}: only directories and .ics files can be published")
            found.add(path.relative_to(stage))
    return found


def _served_paths(release: Path) -> set[Path]:
    """Every .ics file in a served release, none if it is gone. Other read errors raise."""
    try:
        release.stat()
    except FileNotFoundError:
        return set()
    return {
        Path(root, name).relative_to(release)
        for root, _, files in os.walk(release, onerror=_raise)
        for name in files
        if name.endswith(".ics")
    }


def _raise(error: OSError) -> None:
    raise error


def _set_modes(tree: Path, files: int, dirs: int) -> None:
    """Set modes on a tree of plain files and directories, as _feed_paths admits."""
    for root, subdirs, names in os.walk(tree, onerror=_raise):
        for name in names:
            os.chmod(Path(root, name), files)
        for name in subdirs:
            os.chmod(Path(root, name), dirs)
    os.chmod(tree, dirs)


def _ours(path: Path) -> bool:
    return bool(RELEASE_NAME.fullmatch(path.name)) and not path.is_symlink() and path.is_dir()
