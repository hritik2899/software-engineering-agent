"""Repository authorization policy boundary."""
from urllib.parse import urlparse

from minion.config import Settings
from minion.domain import RepositorySpec
from minion.errors import AuthorizationError


class AuthorizationPolicy:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def authorize_repositories(
        self, user_id: str, repositories: list[RepositorySpec]
    ) -> None:
        del user_id
        for repo in repositories:
            parsed = urlparse(repo.url)
            if parsed.scheme not in {"https", "ssh", "git"}:
                raise AuthorizationError(f"unsupported repository URL: {repo.url}")
            if ".." in repo.url:
                raise AuthorizationError("repository URL contains invalid path traversal")
            host = (parsed.hostname or "").lower()
            if self.settings.repo_hosts and host not in self.settings.repo_hosts:
                raise AuthorizationError(
                    f"repository host {host!r} is not in MINION_ALLOWED_REPO_HOSTS"
                )
