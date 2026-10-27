import subprocess

import pytest

from internal.config.instructions import RepositorySourceSpec
from internal.sources.base import SourceUnavailable
from internal.sources.repository import RepositorySource


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "Dev")
    (work / "app").mkdir()
    (work / "app" / "pay.py").write_text("def pay():\n    raise PaymentDeclined('card')\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "first")
    return work


@pytest.fixture
async def source(origin, tmp_path):
    spec = RepositorySourceSpec(
        kind="repository", name="code", description="The product code.", url=f"file://{origin}", branch="main",
        sync_minutes=30,
    )  # fmt: skip
    source = RepositorySource(spec, tmp_path / "clones")
    await source.start()
    return source


async def test_search_and_read(source):
    found = await source.call("search", {"pattern": "PaymentDeclined"})
    assert "app/pay.py:2:" in found.text
    read = await source.call("read", {"path": "app/pay.py"})
    assert read.text.startswith("1: def pay():")


async def test_path_outside_the_clone_is_refused(source):
    for path in ("../../etc/passwd", "/etc/passwd", ".git/config"):
        with pytest.raises(SourceUnavailable):
            await source.call("read", {"path": path})


async def test_after_sync_the_new_commit_is_seen(source, origin):
    (origin / "app" / "fix.py").write_text("FIXED = True\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fix")
    assert "No matches" in (await source.call("search", {"pattern": "FIXED"})).text
    await source.sync()
    assert "app/fix.py" in (await source.call("search", {"pattern": "FIXED"})).text
