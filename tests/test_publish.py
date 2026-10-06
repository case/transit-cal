import os
import shutil
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from transit_cal import publish as publish_module
from transit_cal.publish import PublishError, prune, publish

T0 = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
NAME0 = "20261005T110000.000000Z"


def stage(feeds: Path, name: str, text: str = "BEGIN:VCALENDAR\n") -> Path:
    """A built feed tree, staged the way bin/build-feeds stages it."""
    directory = feeds / "staging" / name
    (directory / "o-test").mkdir(parents=True)
    (directory / "o-test" / "route.ics").write_text(text, encoding="utf-8")
    return directory


def served(feeds: Path) -> str:
    return (feeds / "current" / "o-test" / "route.ics").read_text(encoding="utf-8")


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def at(day: int) -> datetime:
    return T0 + timedelta(days=day)


def release_name(day: int) -> str:
    return at(day).strftime("%Y%m%dT%H%M%S.%fZ")


@pytest.fixture
def feeds(tmp_path: Path) -> Path:
    return tmp_path / "feeds"


def test_first_publish_points_current_at_the_release_with_a_relative_link(feeds: Path):
    release, previous = publish(stage(feeds, "a", "one"), feeds, now=T0)

    assert release == feeds / "releases" / NAME0
    assert previous is None
    assert os.readlink(feeds / "current") == f"releases/{NAME0}"
    assert served(feeds) == "one"
    assert names(feeds / "staging") == []


def test_publish_swaps_current_and_reports_the_release_served_before(feeds: Path):
    first, _ = publish(stage(feeds, "a", "one"), feeds, now=at(0))

    _, previous = publish(stage(feeds, "b", "two"), feeds, now=at(1))

    assert served(feeds) == "two"
    assert previous == first
    assert names(feeds / "releases") == [release_name(0), release_name(1)]
    assert not (feeds / ".current-next").exists()


def test_release_names_use_utc_whatever_the_clock_zone(feeds: Path):
    pacific = T0.astimezone(timezone(timedelta(hours=-7)))

    release, _ = publish(stage(feeds, "a"), feeds, now=pacific)

    assert release.name == NAME0


def test_a_leftover_temporary_link_does_not_block_publishing(feeds: Path):
    feeds.mkdir()
    (feeds / ".current-next").symlink_to("releases/gone")

    publish(stage(feeds, "a", "one"), feeds, now=T0)

    assert served(feeds) == "one"
    assert not (feeds / ".current-next").is_symlink()


def test_naive_clock_is_rejected(feeds: Path):
    with pytest.raises(PublishError, match="timezone"):
        publish(stage(feeds, "a"), feeds, now=T0.replace(tzinfo=None))


def test_stage_without_feeds_is_rejected_and_the_served_release_stays(feeds: Path):
    publish(stage(feeds, "a", "one"), feeds, now=at(0))
    empty = feeds / "staging" / "empty"
    empty.mkdir()

    with pytest.raises(PublishError, match="no .ics feeds"):
        publish(empty, feeds, now=at(1))
    assert served(feeds) == "one"
    assert empty.is_dir()


def test_missing_stage_is_rejected(feeds: Path):
    (feeds / "staging").mkdir(parents=True)

    with pytest.raises(PublishError, match="no .ics feeds"):
        publish(feeds / "staging" / "nope", feeds, now=T0)


def test_stage_outside_feeds_staging_is_rejected(tmp_path: Path, feeds: Path):
    outside = stage(tmp_path / "elsewhere", "a")

    with pytest.raises(PublishError, match="a stage must be a directory in"):
        publish(outside, feeds, now=T0)
    assert outside.is_dir()
    assert not feeds.exists()


def test_symlinked_stage_is_rejected(tmp_path: Path, feeds: Path):
    real = stage(tmp_path / "elsewhere", "a")
    link = feeds / "staging" / "link"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    with pytest.raises(PublishError, match="a stage must be a directory in"):
        publish(link, feeds, now=T0)
    assert link.is_symlink()
    assert not (feeds / "releases").exists()


def test_current_that_is_not_a_symlink_is_never_replaced(feeds: Path):
    (feeds / "current").mkdir(parents=True)
    staged = stage(feeds, "a")

    with pytest.raises(PublishError, match="not a symlink"):
        publish(staged, feeds, now=T0)
    assert (feeds / "current").is_dir()
    assert staged.is_dir()


def test_same_release_name_twice_is_rejected_and_the_served_release_stays(feeds: Path):
    publish(stage(feeds, "a", "one"), feeds, now=T0)
    staged = stage(feeds, "b", "two")

    with pytest.raises(PublishError, match="already exists"):
        publish(staged, feeds, now=T0)
    assert served(feeds) == "one"
    assert staged.is_dir()


@pytest.mark.parametrize("failing", ["symlink_to", "replace"])
def test_a_failed_swap_puts_the_stage_back_and_leaves_current(
    feeds: Path, monkeypatch: pytest.MonkeyPatch, failing: str
):
    publish(stage(feeds, "a", "one"), feeds, now=at(0))
    staged = stage(feeds, "b", "two")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    if failing == "symlink_to":
        monkeypatch.setattr(Path, "symlink_to", fail)
    else:
        monkeypatch.setattr(publish_module.os, "replace", fail)

    with pytest.raises(OSError, match="disk full"):
        publish(staged, feeds, now=at(1))
    assert served(feeds) == "one"
    assert (staged / "o-test" / "route.ics").read_text(encoding="utf-8") == "two"
    assert names(feeds / "releases") == [release_name(0)]
    assert not (feeds / ".current-next").is_symlink()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_published_release_is_read_only(feeds: Path):
    release, _ = publish(stage(feeds, "a", "one"), feeds, now=T0)

    assert mode(release) == 0o555
    assert mode(release / "o-test") == 0o555
    assert mode(release / "o-test" / "route.ics") == 0o444


@pytest.mark.parametrize(
    "extra",
    ["notes.txt", "o-test/.route.ics.tmp", "o-test/link.ics", "o-test/dir-link", "o-test/pipe.ics"],
)
def test_a_stage_holding_anything_but_directories_and_ics_files_is_rejected(
    tmp_path: Path, feeds: Path, extra: str
):
    publish(stage(feeds, "a", "one"), feeds, now=at(0))
    staged = stage(feeds, "b", "two")
    path = staged / extra
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key").write_text("secret", encoding="utf-8")
    if extra.endswith("link.ics"):
        path.symlink_to(outside / "key")
    elif extra.endswith("dir-link"):
        path.symlink_to(outside)
    elif extra.endswith("pipe.ics"):
        os.mkfifo(path)
    else:
        path.write_text("x", encoding="utf-8")

    with pytest.raises(PublishError, match="only directories and .ics files"):
        publish(staged, feeds, now=at(1))
    assert served(feeds) == "one"
    assert staged.is_dir()


def test_a_stage_that_drops_a_published_feed_is_rejected(feeds: Path):
    first = stage(feeds, "a", "one")
    (first / "o-test" / "other.ics").write_text("other", encoding="utf-8")
    publish(first, feeds, now=at(0))
    staged = stage(feeds, "b", "two")

    with pytest.raises(PublishError, match=r"would drop the published feed o-test/other\.ics"):
        publish(staged, feeds, now=at(1))
    assert served(feeds) == "one"
    assert staged.is_dir()


def test_a_stage_may_add_feeds(feeds: Path):
    publish(stage(feeds, "a", "one"), feeds, now=at(0))
    staged = stage(feeds, "b", "two")
    (staged / "o-new").mkdir()
    (staged / "o-new" / "route.ics").write_text("new", encoding="utf-8")

    publish(staged, feeds, now=at(1))

    assert (feeds / "current" / "o-new" / "route.ics").read_text(encoding="utf-8") == "new"


def test_a_dangling_current_skips_the_dropped_feed_check(feeds: Path):
    feeds.mkdir()
    (feeds / "current").symlink_to("releases/20200101T000000.000000Z")

    publish(stage(feeds, "a", "one"), feeds, now=T0)

    assert served(feeds) == "one"


def test_an_unreadable_served_release_fails_closed(feeds: Path):
    release, _ = publish(stage(feeds, "a", "one"), feeds, now=at(0))
    staged = stage(feeds, "b", "two")
    (release / "o-test").chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            publish(staged, feeds, now=at(1))
    finally:
        (release / "o-test").chmod(0o555)
    assert served(feeds) == "one"
    assert staged.is_dir()


def test_a_failed_swap_leaves_the_restored_stage_writable(
    feeds: Path, monkeypatch: pytest.MonkeyPatch
):
    staged = stage(feeds, "a", "one")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(publish_module.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        publish(staged, feeds, now=T0)

    assert mode(staged) == 0o755
    assert mode(staged / "o-test" / "route.ics") == 0o644
    shutil.rmtree(staged)


def test_prune_deletes_read_only_releases(feeds: Path):
    for day in range(3):
        publish(stage(feeds, f"s{day}", str(day)), feeds, now=at(day))
    releases = feeds / "releases"
    newest, previous = releases / release_name(2), releases / release_name(1)

    deleted = prune(releases, keep=1, spare=(newest, previous))

    assert deleted == [releases / release_name(0)]
    assert mode(newest) == 0o555


def make_releases(feeds: Path, days: range) -> list[Path]:
    made = [feeds / "releases" / release_name(d) for d in days]
    for path in made:
        path.mkdir(parents=True)
    return made


def test_prune_deletes_the_oldest_beyond_keep(feeds: Path):
    made = make_releases(feeds, range(4))

    deleted = prune(feeds / "releases", keep=2, spare=(made[3], made[2]))

    assert deleted == made[:2]
    assert names(feeds / "releases") == [made[2].name, made[3].name]


def test_prune_spares_the_previous_release_even_with_keep_1(feeds: Path):
    made = make_releases(feeds, range(3))

    prune(feeds / "releases", keep=1, spare=(made[2], made[1]))

    assert names(feeds / "releases") == [made[1].name, made[2].name]


def test_prune_spares_the_served_release_when_the_clock_went_backwards(feeds: Path):
    made = make_releases(feeds, range(3))

    prune(feeds / "releases", keep=1, spare=(made[0], None))

    assert names(feeds / "releases") == [made[0].name, made[2].name]


def test_prune_leaves_entries_it_did_not_create(tmp_path: Path, feeds: Path):
    made = make_releases(feeds, range(1))
    releases = feeds / "releases"
    (releases / "notes").mkdir()
    (releases / "readme.txt").write_text("keep me", encoding="utf-8")
    (releases / "20261005T110000.00000٠Z").mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (releases / "20200101T000000.000000Z").symlink_to(target)
    (releases / "20190101T000000.000000Z").write_text("a file", encoding="utf-8")

    assert prune(releases, keep=1, spare=(made[0], None)) == []
    assert target.is_dir()
    assert len(names(releases)) == 6


@pytest.mark.parametrize("keep", [0, -1])
def test_prune_keep_below_1_is_rejected_before_deleting(feeds: Path, keep: int):
    made = make_releases(feeds, range(3))

    with pytest.raises(PublishError, match="keep"):
        prune(feeds / "releases", keep=keep, spare=(made[2], None))
    assert len(names(feeds / "releases")) == 3


def test_prune_failure_propagates(feeds: Path, monkeypatch: pytest.MonkeyPatch):
    made = make_releases(feeds, range(3))

    def fail(path: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(shutil, "rmtree", fail)

    with pytest.raises(OSError, match="busy"):
        prune(feeds / "releases", keep=1, spare=(made[2], made[1]))
