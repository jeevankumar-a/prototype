import re
from urllib.parse import unquote, urlsplit

from mcp import ClientSession, types


class GitHubMCPError(RuntimeError):
    pass


def _messages(result: types.CallToolResult) -> str:
    return "\n".join(item.text for item in result.content if isinstance(item, types.TextContent))


class GitHubPublisher:
    def __init__(self, session: ClientSession, repo: str, branch: str, directory: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("TARGET_REPO must be owner/repository")
        if not branch or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", directory):
            raise ValueError("Invalid target branch or directory")
        if any(part in (".", "..") for part in directory.split("/")):
            raise ValueError("Invalid target directory")
        self.owner, self.repo = repo.split("/")
        self.branch, self.directory, self.session = branch, directory, session

    def path(self, digest: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid snapshot digest")
        return f"{self.directory}/{digest}.mmd"

    async def exists_with_content(self, path: str, content: str) -> bool:
        result = await self.session.call_tool("get_file_contents", {
            "owner": self.owner, "repo": self.repo, "path": path, "ref": f"refs/heads/{self.branch}",
        })
        message = _messages(result)
        if result.isError:
            if message == ("Failed to get file contents. The path does not point to a file or "
                           "directory, or the file does not exist in the repository."):
                return False
            raise GitHubMCPError(f"GitHub MCP read failed: {message}")
        if "default branch" in message.lower() and "instead" in message.lower():
            raise GitHubMCPError("GitHub MCP fell back to the default branch")
        resources = [item.resource for item in result.content if isinstance(item, types.EmbeddedResource)]
        if len(resources) != 1 or not isinstance(resources[0], types.TextResourceContents):
            raise GitHubMCPError("GitHub MCP did not return the requested text file")
        resource = resources[0]
        uri = urlsplit(str(resource.uri))
        parts = unquote(uri.path).split("/")
        expected = ["", self.repo, "sha"]
        if (uri.scheme != "repo" or uri.netloc.lower() != self.owner.lower()
                or len(parts) < 6 or [p.lower() for p in parts[:3]] != [p.lower() for p in expected]
                or not re.fullmatch(r"[0-9a-fA-F]{40}", parts[3])
                or parts[4] != "contents" or "/".join(parts[5:]) != path):
            raise GitHubMCPError("GitHub MCP returned a different repository path")
        if resource.text != content:
            raise ValueError(f"GitHub path already exists with different content: {path}")
        return True

    async def publish(self, path: str, content: str, message: str) -> str:
        if await self.exists_with_content(path, content):
            return path
        result = await self.session.call_tool("create_or_update_file", {
            "owner": self.owner, "repo": self.repo, "path": path,
            "branch": self.branch, "content": content, "message": message,
        })
        if result.isError:
            if await self.exists_with_content(path, content):
                return path
            raise GitHubMCPError(f"GitHub MCP publish failed: {_messages(result)}")
        if not await self.exists_with_content(path, content):
            raise GitHubMCPError("GitHub MCP did not confirm the published file")
        return path
