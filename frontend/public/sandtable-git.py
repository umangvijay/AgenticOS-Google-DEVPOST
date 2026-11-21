#!/usr/bin/env python3
"""Run a Sandtable Git request in a trusted checkout on your own device."""
import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def destination(raw):
    url = urlsplit(raw)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or not url.path.strip("/")):
        raise ValueError("Use an HTTPS repository URL without credentials, query or fragment.")
    return (url.hostname.lower(), url.port or 443, url.path.rstrip("/").removesuffix(".git"))


def execute(request, repo, remote, confirm=input):
    action = request["operation"]
    if action not in {"fetch", "pull", "push"}:
        raise ValueError("Unsupported Git operation.")

    def git(*args, capture=False):
        result = subprocess.run(["git", "-C", str(repo), "-c", "push.followTags=false", *args],
                                check=True, text=True, stdout=subprocess.PIPE if capture else None)
        return result.stdout.strip() if capture else None

    if remote.startswith("-") or remote not in git("remote", capture=True).splitlines():
        raise ValueError("Select a configured remote in this checkout.")
    urls = git("remote", "get-url", "--all", remote, capture=True).splitlines()
    push_urls = git("remote", "get-url", "--push", "--all", remote, capture=True).splitlines()
    expected = destination(request["url"])
    if len(urls) != 1 or len(push_urls) != 1 or any(destination(u) != expected for u in urls + push_urls):
        raise ValueError("The configured fetch and push destinations must match the requested repository.")
    branch = git("symbolic-ref", "--short", "HEAD", capture=True)
    if request.get("branch") and request["branch"] != branch:
        raise ValueError("Switch to the requested branch first, then retry.")
    print(f"{action.upper()} {repo.resolve()} · {remote} · {request['url']} · branch {branch}")
    git("status", "--short")
    if confirm(f"Type {action} to authorize: ").strip() != action:
        raise ValueError("Cancelled.")
    if action == "fetch":
        git("fetch", "--no-tags", remote, f"refs/heads/{branch}")
    elif action == "pull":
        if git("status", "--porcelain", capture=True):
            raise ValueError("Commit or stash local changes before pulling.")
        git("pull", "--ff-only", "--no-tags", remote, branch)
    else:
        staged = git("diff", "--cached", "--name-status", capture=True)
        if staged:
            print("Staged files:\n" + staged)
            message = confirm("Commit these staged changes before pushing? Enter a commit message, or leave blank to cancel: ").strip()
            if not message:
                raise ValueError("Cancelled.")
            git("diff", "--cached", "--check")
            git("commit", "-m", message)
        elif git("status", "--porcelain", capture=True):
            raise ValueError("Stage the files or folders you intend to publish, then retry.")
        git("push", "--no-follow-tags", remote, f"HEAD:refs/heads/{branch}")
        refs = git("ls-remote", "--heads", remote, f"refs/heads/{branch}", capture=True).split()
        if not refs or refs[0] != git("rev-parse", "HEAD", capture=True):
            raise ValueError("Push returned, but remote HEAD verification failed.")
    print(f"{action.capitalize()} verified at", git("rev-parse", "FETCH_HEAD" if action == "fetch" else "HEAD", capture=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()
    try:
        execute(json.loads(args.request.read_text()), args.repo, args.remote)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Git request stopped: {error}\n")
