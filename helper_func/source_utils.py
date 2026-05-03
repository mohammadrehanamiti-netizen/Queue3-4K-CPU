from urllib.parse import urlparse

DM_HOSTS = ("dailymotion.com", "www.dailymotion.com", "dai.ly", "www.dai.ly", "dmcdn.net")
DM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DM_REFERER = "https://www.dailymotion.com/"


def is_dailymotion_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == dm_host or host.endswith(f".{dm_host}") for dm_host in DM_HOSTS)


def normalize_video_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host in ("dailymotion.com", "www.dailymotion.com") and path.startswith("/embed/video/"):
        video_id = path.split("/embed/video/", 1)[1].split("/", 1)[0]
        if video_id:
            return f"https://www.dailymotion.com/video/{video_id}"

    if host in ("dai.ly", "www.dai.ly"):
        video_id = path.strip("/").split("/", 1)[0]
        if video_id:
            return f"https://www.dailymotion.com/video/{video_id}"

    return url


def build_ytdlp_base_command(url: str):
    normalized_url = normalize_video_url(url)
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist"]

    if is_dailymotion_url(url):
        cmd.extend(["--user-agent", DM_UA, "--referer", DM_REFERER])

    return cmd, normalized_url
