"""Authorization policy boundary.

The local implementation is intentionally simple but the orchestration layer never
bypasses this interface.  A production deployment can replace it with an IAM/ACL
service that verifies repository read/write and PR permissions for the initiating
user.
"""
from urllib.parse import urlparse

from minion.domain import RepositorySpec
from minion.errors import AuthorizationError


class AuthorizationPolicy:
    async def authorize_repositories(
        self, user_id: str, repositories: list[RepositorySpec]
    ) -> None:
        for repo in repositories:
            parsed = urlparse(repo.url)
            if parsed.scheme not in {"https", "ssh", "git"}:
                raise AuthorizationError(f"unsupported repository URL: {repo.url}")
            if ".." in repo.url:
                raise AuthorizationError("repository URL contains invalid path traversal")
