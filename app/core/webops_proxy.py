"""WebOps proxy -- authenticated API proxy to Vercel, Cloudflare, Supabase, GitHub, Coolify."""

from __future__ import annotations

import httpx


class WebOpsProxy:
    """Proxy API requests to external web services with token auth."""

    BASE_URLS: dict[str, str] = {
        "vercel": "https://api.vercel.com",
        "cloudflare": "https://api.cloudflare.com/client/v4",
        "github": "https://api.github.com",
        "supabase": "",  # set from config
        "coolify": "",   # set from config
    }

    def __init__(
        self,
        vercel_token: str = "",
        cloudflare_token: str = "",
        github_token: str = "",
        supabase_url: str = "",
        supabase_token: str = "",
        coolify_url: str = "",
        coolify_token: str = "",
    ) -> None:
        self._tokens = {
            "vercel": vercel_token,
            "cloudflare": cloudflare_token,
            "github": github_token,
            "supabase": supabase_token,
            "coolify": coolify_token,
        }
        self.BASE_URLS = dict(self.__class__.BASE_URLS)
        self.BASE_URLS["supabase"] = supabase_url
        self.BASE_URLS["coolify"] = coolify_url

    def get_headers(self, service: str) -> dict[str, str]:
        token = self._tokens.get(service, "")
        if not token:
            return {}
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if service == "github":
            headers["Accept"] = "application/vnd.github+json"
        if service == "supabase":
            headers["apikey"] = token
        return headers

    def get_base_url(self, service: str) -> str:
        return self.BASE_URLS.get(service, "")

    def available_services(self) -> list[str]:
        return list(self._tokens.keys())

    async def request(
        self,
        service: str,
        method: str,
        path: str,
        body: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        base_url = self.get_base_url(service)
        headers = self.get_headers(service)
        if extra_headers:
            headers.update(extra_headers)
        url = f"{base_url}{path}"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text[:100000],
        }
