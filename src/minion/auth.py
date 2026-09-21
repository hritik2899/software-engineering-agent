"""Repository authorization policy boundary."""
from urllib.parse import urlparse

from minion.domain import RepositorySpec
from minion.errors import AuthorizationError


class AuthorizationPolicy:
    """Minimal local policy behind the same boundary as a real IAM integration."""

    def __init__(self, allow_local_repositories: bool = False):
        self.allow_local_repositories = allow_local_repositories

    async def authorize_repositories(
        self,
        user_id: str,
        repositories: list[RepositorySpec],
    ) -> None:
        if not user_id:
            raise AuthorizationError("user_id is required")

        allowed = {"https", "ssh", "git"}
        if self.allow_local_repositories:
            allowed.add("file")

        for repository in repositories:
            parsed = urlparse(repository.url)
            if parsed.scheme not in allowed:
                raise AuthorizationError(
                    f"unsupported repository URL: {repository.url}"
                )
            if ".." in repository.url:
                raise AuthorizationError(
                    "repository URL contains invalid path traversal"
                )
