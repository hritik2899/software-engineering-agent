"""Backend-owned GitHub publication service.

The agent edits and verifies a local task branch; this service separately owns
authenticated push and pull-request creation. That separation prevents model-driven
shell commands from receiving the GitHub token and keeps PR creation idempotent.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from minion.config import Settings
from minion.domain import RepositorySpec
from minion.git_auth import git_environment
from minion.runtime.workspace import Workspace, repo_name


@dataclass(slots=True)
class PullRequestResult:
    repository: str
    url: str


def _github_slug(url: str) -> str:
    match = re.search(r"github\\.com[:/]([^/]+)/([^/]+?)(?:\\.git)?$", url.rstrip("/"))
    if not match:
        raise ValueError(f"not a GitHub repository URL: {url}")
    return f"{match.group(1)}/{match.group(2)}"


class GitHubPublisher:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def publish(
        self,
        workspace: Workspace,
        task_id: str,
        instruction: str,
        specs: list[RepositorySpec],
    ) -> list[PullRequestResult]:
        if not self.settings.github_token:
            raise RuntimeError("GITHUB_TOKEN is required when publish_pr=true")

        results: list[PullRequestResult] = []
        headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
        }
        git_env = git_environment(self.settings)

        async with httpx.AsyncClient(
            base_url=self.settings.github_api_url,
            headers=headers,
            timeout=30,
        ) as client:
            for spec in specs:
                name = repo_name(spec)
                repo = workspace.repo_path(name)
                branch_proc = await asyncio.create_subprocess_exec(
                    "git",
                    "branch",
                    "--show-current",
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                )
                branch_out, _ = await branch_proc.communicate()
                branch = branch_out.decode().strip()

                push = await asyncio.create_subprocess_exec(
                    "git",
                    "push",
                    "-u",
                    "origin",
                    branch,
                    cwd=str(repo),
                    env=git_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                push_out, _ = await push.communicate()
                if push.returncode != 0:
                    raise RuntimeError(push_out.decode(errors="replace"))

                slug = _github_slug(spec.url)
                existing = await client.get(
                    f"/repos/{slug}/pulls",
                    params={"state": "open", "head": f"{slug.split('/')[0]}:{branch}"},
                )
                existing.raise_for_status()
                pulls = existing.json()
                if pulls:
                    results.append(PullRequestResult(slug, pulls[0]["html_url"]))
                    continue

                response = await client.post(
                    f"/repos/{slug}/pulls",
                    json={
                        "title": f"Agent task {task_id}",
                        "head": branch,
                        "base": spec.base_branch,
                        "body": (
                            f"Automated change for:\\n\\n{instruction}"
                            f"\\n\\nTask: {task_id}"
                        ),
                    },
                )
                response.raise_for_status()
                results.append(PullRequestResult(slug, response.json()["html_url"]))
        return results
