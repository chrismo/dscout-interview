"""Disposable Git history, independent of the exercise checkout."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

import os
from pathlib import Path
import subprocess


def git(repo, *args):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="CI fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="CI fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
    )
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
         "-C", str(repo), *args], env=env, text=True,
    ).strip()


APPS = ("zorch", "greeb", "blerg")


def validate_apps(apps):
    apps = tuple(apps)
    for app in apps:
        if app not in APPS:
            raise ValueError(f"unknown app: {app}")
    return tuple(app for app in APPS if app in apps)


def add_changes(repo, apps):
    apps = validate_apps(apps)
    repo = Path(repo)
    sources = {}
    for app in apps:
        path = repo / "source" / f"{app}.txt"
        version = int(path.read_text().strip().removeprefix(f"{app}-v"))
        sources[path] = f"{app}-v{version + 1}\n"
    for path, content in sources.items():
        path.write_text(content)
    if apps:
        git(repo, "add", "--", *(f"source/{app}.txt" for app in apps))
    message = "change " + ", ".join(apps) if apps else "no source changes"
    git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def create_history(repo):
    repo = Path(repo)
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=fixture")
    source = repo / "source"
    source.mkdir()
    for app in APPS:
        (source / f"{app}.txt").write_text(f"{app}-v1\n")
    git(repo, "add", "source")
    git(repo, "commit", "--quiet", "-m", "existing apps")
    return [add_changes(repo, apps) for apps in (APPS, ("greeb",), ("zorch",))]
