import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlencode

import requests
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
    SpinnerColumn,
    TaskProgressColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# ─── Constants ───────────────────────────────────────────────────────────────

# Public bearer token embedded in X's JavaScript — same for everyone, not a secret.
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

console = Console()


# ─── Dynamic GraphQL Endpoint Resolution ─────────────────────────────────────

def resolve_graphql_endpoints(session_or_headers=None) -> dict[str, str]:
    """Extracts current GraphQL query IDs from X's JavaScript bundles.

    X changes these IDs frequently, so we extract them at runtime.
    Returns a dict like: {"UserByScreenName": "https://...", "UserMedia": "https://..."}
    """
    target_operations = ["UserByScreenName", "UserMedia"]
    endpoints = {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    console.print("[dim]Resolving GraphQL endpoints from X's JS...[/dim]", end="")

    try:
        # Step 1: Fetch the main page to find JS bundle URLs
        resp = requests.get("https://x.com", headers=headers, timeout=15)
        resp.raise_for_status()

        # Find all JS bundle URLs — they look like:
        # https://abs.twimg.com/responsive-web/client-web/main.XXXXXXXX.js
        js_urls = re.findall(
            r'(https://abs\.twimg\.com/responsive-web/client-web[^"\s]+\.js)',
            resp.text,
        )

        if not js_urls:
            # Fallback pattern for alternative CDN
            js_urls = re.findall(
                r'(https://[^"\s]+/client-web[^"\s]+\.js)',
                resp.text,
            )

        # Step 2: Search each JS bundle for queryId + operationName pairs
        # The pattern in the JS is typically:
        #   {queryId:"AbCdEf123",operationName:"UserMedia",...}
        query_pattern = re.compile(
            r'queryId:\s*"([^"]+)"\s*,\s*operationName:\s*"([^"]+)"'
        )

        for js_url in js_urls:
            if len(endpoints) == len(target_operations):
                break

            try:
                js_resp = requests.get(js_url, headers=headers, timeout=15)
                js_resp.raise_for_status()
                js_text = js_resp.text

                for match in query_pattern.finditer(js_text):
                    query_id = match.group(1)
                    op_name = match.group(2)
                    if op_name in target_operations and op_name not in endpoints:
                        endpoints[op_name] = f"https://x.com/i/api/graphql/{query_id}/{op_name}"

            except requests.exceptions.RequestException:
                continue

    except requests.exceptions.RequestException as e:
        console.print(f" [red]failed[/red]")
        raise RuntimeError(
            f"Could not fetch X's JavaScript bundles to resolve endpoints: {e}"
        )

    if len(endpoints) < len(target_operations):
        missing = [op for op in target_operations if op not in endpoints]
        console.print(f" [red]failed[/red]")
        raise RuntimeError(
            f"Could not find GraphQL query IDs for: {', '.join(missing)}.\n"
            "X may have changed their JS bundle structure."
        )

    console.print(" [green]OK[/green]")
    for op, url in endpoints.items():
        qid = url.split("/graphql/")[1].split("/")[0]
        console.print(f"  [dim]{op} -> {qid}[/dim]")

    return endpoints


# ─── HTTP Session ────────────────────────────────────────────────────────────

class XSession:
    """Manages an authenticated session with X's internal API."""

    def __init__(self, ct0: str, auth_token: str):
        self.session = requests.Session()
        self.ct0 = ct0
        self.auth_token = auth_token

        self.session.cookies.set("ct0", ct0, domain=".x.com")
        self.session.cookies.set("auth_token", auth_token, domain=".x.com")

        self.session.headers.update({
            "Authorization": f"Bearer {BEARER_TOKEN}",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://x.com/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def get(self, url: str, params: dict = None) -> dict:
        """Makes a GET request and returns JSON, with retry logic."""
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 60))
                    wait = max(reset - int(time.time()), 5)
                    console.print(f"[yellow]⏳ Rate limited. Waiting {wait}s...[/yellow]")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise
                console.print(f"[yellow]⚠ Request failed (attempt {attempt+1}/3): {e}[/yellow]")
                time.sleep(2 ** attempt)
        return {}


# ─── User Info ───────────────────────────────────────────────────────────────

def get_user_id(session: XSession, endpoint_url: str, username: str) -> tuple[str, str]:
    """Resolves a username to (rest_id, display_name)."""
    variables = {
        "screen_name": username,
        "withSafetyModeUserFields": True,
    }
    features = {
        "hidden_profile_subscriptions_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": True,
        "subscriptions_feature_can_gift_premium": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }
    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(features),
    }
    data = session.get(endpoint_url, params=params)
    user = data.get("data", {}).get("user", {}).get("result", {})
    rest_id = user.get("rest_id")
    name = user.get("legacy", {}).get("name", username)
    if not rest_id:
        raise ValueError(f"Could not find user @{username}. Check the username and your cookies.")
    return rest_id, name


# ─── Media Fetching ──────────────────────────────────────────────────────────

def _build_user_media_variables(user_id: str, cursor: str = None, count: int = 20) -> dict:
    variables = {
        "userId": user_id,
        "count": count,
        "includePromotedContent": False,
        "withClientEventToken": False,
        "withBirdwatchNotes": False,
        "withVoice": True,
        "withV2Timeline": True,
    }
    if cursor:
        variables["cursor"] = cursor
    return variables


def _build_features() -> dict:
    return {
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }


def _build_field_toggles() -> dict:
    return {
        "withArticlePlainText": False,
    }


def _extract_media_from_entry(entry: dict) -> list[dict]:
    """Extracts media items (url, type, tweet_id) from a timeline entry."""
    media_items = []

    def _walk(obj):
        """Recursively walk the JSON to find tweet results with media."""
        if isinstance(obj, dict):
            # Look for tweet result objects
            if "legacy" in obj and "entities" in obj.get("legacy", {}):
                legacy = obj["legacy"]
                tweet_id = legacy.get("id_str", "")
                created_at = legacy.get("created_at", "")
                extended = legacy.get("extended_entities", {})
                medias = extended.get("media", [])
                for m in medias:
                    media_type = m.get("type", "photo")
                    if media_type == "photo":
                        url = m.get("media_url_https", "")
                        if url:
                            # Request original quality
                            orig_url = f"{url}?name=orig"
                            media_items.append({
                                "url": orig_url,
                                "type": "photo",
                                "tweet_id": tweet_id,
                                "created_at": created_at,
                            })
                    elif media_type in ("video", "animated_gif"):
                        variants = m.get("video_info", {}).get("variants", [])
                        # Pick highest bitrate mp4
                        mp4_variants = [
                            v for v in variants
                            if v.get("content_type") == "video/mp4"
                        ]
                        if mp4_variants:
                            best = max(mp4_variants, key=lambda v: v.get("bitrate", 0))
                            media_items.append({
                                "url": best["url"],
                                "type": "video" if media_type == "video" else "gif",
                                "tweet_id": tweet_id,
                                "created_at": created_at,
                                "bitrate": best.get("bitrate", 0),
                            })
                return  # Don't recurse into already-processed legacy
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(entry)
    return media_items


def fetch_all_media(session: XSession, endpoint_url: str, user_id: str, username: str, debug: bool = False) -> list[dict]:
    """Fetches all media items from a user's /media timeline, handling pagination."""
    all_media = []
    cursor = None
    page = 0
    seen_urls = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Scanning media timeline..."),
        TextColumn("[green]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("", total=None)

        while True:
            page += 1
            variables = _build_user_media_variables(user_id, cursor)
            features = _build_features()
            field_toggles = _build_field_toggles()
            params = {
                "variables": json.dumps(variables),
                "features": json.dumps(features),
                "fieldToggles": json.dumps(field_toggles),
            }

            data = session.get(endpoint_url, params=params)

            # Debug: dump raw response on first page
            if debug and page == 1:
                debug_path = Path("debug_response.json")
                with open(debug_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                console.print(f"\n[yellow]DEBUG: Raw API response saved to {debug_path.resolve()}[/yellow]")
                # Also print the top-level keys for quick diagnosis
                console.print(f"[yellow]DEBUG: Top-level keys: {list(data.keys())}[/yellow]")
                if "data" in data:
                    console.print(f"[yellow]DEBUG: data keys: {list(data['data'].keys()) if isinstance(data['data'], dict) else type(data['data'])}[/yellow]")
                    user_data = data.get("data", {}).get("user", {})
                    if user_data:
                        result = user_data.get("result", {})
                        console.print(f"[yellow]DEBUG: result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}[/yellow]")
                        timeline_v2 = result.get("timeline_v2", result.get("timeline", {}))
                        console.print(f"[yellow]DEBUG: timeline keys: {list(timeline_v2.keys()) if isinstance(timeline_v2, dict) else type(timeline_v2)}[/yellow]")
                if "errors" in data:
                    console.print(f"[red]DEBUG: API errors: {data['errors']}[/red]")

            timeline = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
            )
            # Handle both timeline_v2 and timeline keys
            timeline_obj = timeline.get("timeline_v2", timeline.get("timeline", {}))
            if isinstance(timeline_obj, dict) and "timeline" in timeline_obj:
                timeline_obj = timeline_obj["timeline"]
            instructions = timeline_obj.get("instructions", []) if isinstance(timeline_obj, dict) else []

            entries = []
            for instruction in instructions:
                inst_type = instruction.get("type")
                if inst_type == "TimelineAddEntries":
                    entries.extend(instruction.get("entries", []))
                elif inst_type == "TimelineAddToModule":
                    entries.extend(instruction.get("moduleItems", []))

            if not entries:
                if debug:
                    console.print(f"[yellow]DEBUG: No entries found. Instructions: {[i.get('type') for i in instructions]}[/yellow]")
                    if instructions:
                        # Dump first instruction keys
                        console.print(f"[yellow]DEBUG: First instruction keys: {list(instructions[0].keys())}[/yellow]")
                break

            next_cursor = None
            new_in_page = 0
            for entry in entries:
                entry_id = entry.get("entryId", "")

                # Handle cursor entries
                if "cursor-bottom" in entry_id:
                    next_cursor = (
                        entry.get("content", {})
                        .get("value")
                        or entry.get("content", {})
                        .get("itemContent", {})
                        .get("value")
                    )
                    continue
                if "cursor-top" in entry_id:
                    continue

                # Extract media
                items = _extract_media_from_entry(entry)
                for item in items:
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        all_media.append(item)
                        new_in_page += 1

            progress.update(
                task,
                description=f"Page {page} | {len(all_media)} media found (+{new_in_page})",
            )

            if not next_cursor or new_in_page == 0:
                break

            cursor = next_cursor
            time.sleep(1.5)  # Respect rate limits

    return all_media


# ─── Downloading ─────────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def _get_filename(item: dict, index: int) -> str:
    """Generates a descriptive filename for a media item."""
    url = item["url"].split("?")[0]
    ext = Path(urlparse(url).path).suffix or ".jpg"

    # For videos, clean up the extension (remove resolution tags)
    if item["type"] in ("video", "gif"):
        ext = ".mp4"

    tweet_id = item.get("tweet_id", f"unknown_{index}")
    media_type = item["type"]
    return _sanitize_filename(f"{tweet_id}_{media_type}_{index}{ext}")


def download_file(
    item: dict,
    index: int,
    output_dir: Path,
    progress: Progress,
    overall_task,
) -> tuple[bool, str]:
    """Downloads a single media file. Returns (success, filename)."""
    filename = _get_filename(item, index)

    # Route into subfolders: Fotos/ or Videos/
    if item["type"] == "photo":
        sub_dir = output_dir / "Fotos"
    else:
        sub_dir = output_dir / "Videos"
    sub_dir.mkdir(parents=True, exist_ok=True)

    filepath = sub_dir / filename

    if filepath.exists():
        progress.advance(overall_task)
        return True, filename

    url = item["url"]

    try:
        resp = requests.get(url, stream=True, timeout=60, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://x.com/",
        })
        resp.raise_for_status()

        total_size = int(resp.headers.get("content-length", 0))
        dl_task = progress.add_task(
            f"  [cyan]{filename[:50]}",
            total=total_size or None,
            visible=True,
        )

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                progress.advance(dl_task, len(chunk))

        progress.update(dl_task, visible=False)
        progress.advance(overall_task)
        return True, filename

    except Exception as e:
        progress.advance(overall_task)
        return False, f"{filename} ({e})"


def download_all_media(
    media_items: list[dict],
    output_dir: Path,
    max_threads: int = 4,
) -> tuple[int, int]:
    """Downloads all media items with a thread pool. Returns (success_count, fail_count)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    successes = 0
    failures = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=40),
        TaskProgressColumn(),
        TextColumn("•"),
        DownloadColumn(),
        TextColumn("•"),
        TransferSpeedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall = progress.add_task(
            f"[green]Downloading {len(media_items)} files ({max_threads} threads)",
            total=len(media_items),
        )

        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = {
                executor.submit(
                    download_file, item, i, output_dir, progress, overall
                ): item
                for i, item in enumerate(media_items)
            }
            for future in as_completed(futures):
                ok, name = future.result()
                if ok:
                    successes += 1
                else:
                    failures.append(name)

    return successes, failures


# ─── CLI ─────────────────────────────────────────────────────────────────────

def load_cookies_from_file(path: str) -> tuple[str, str]:
    """Loads ct0 and auth_token from a text file.

    Supported formats:
        ct0=VALUE
        auth_token=VALUE
    """
    ct0, auth_token = None, None
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ct0="):
                ct0 = line.split("=", 1)[1].strip()
            elif line.startswith("auth_token="):
                auth_token = line.split("=", 1)[1].strip()
    if not ct0 or not auth_token:
        raise ValueError(
            "Cookie file must contain both 'ct0=VALUE' and 'auth_token=VALUE' lines."
        )
    return ct0, auth_token


DEFAULT_COOKIES_PATH = Path(__file__).parent / ".cookies"


def login_with_selenium(save_path: Path = None) -> tuple[str, str]:
    """Opens an undetected Chrome browser for the user to log in manually.

    Uses undetected-chromedriver to bypass X's bot detection.
    Waits until the ct0 and auth_token cookies appear, then returns them.
    Cookies are automatically saved to a file for future sessions.
    """
    try:
        import undetected_chromedriver as uc
    except ImportError:
        console.print(
            "[bold red]Error:[/bold red] undetected-chromedriver is required for --login.\n"
            "Install it with: [bold]pip install undetected-chromedriver[/bold]"
        )
        sys.exit(1)

    save_path = save_path or DEFAULT_COOKIES_PATH

    console.print()
    console.print(
        Panel(
            "[bold]Uma janela do Chrome vai abrir (undetected mode).\n"
            "Entre na sua conta do X normalmente.\n"
            "Essa janela vai fechar automaticamente assim que o login for detectado.[/bold]\n\n"
            "[dim]Esse processo usa undetected-chromedriver para evitar a detecção de bots.\n"
            "Feche TODAS as outras janelas do Chrome antes de continuar para melhores resultados.[/dim]",
            title="[cyan]Login Manual[/cyan]",
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()

    # ── Launch undetected Chrome ──
    try:
        chrome_options = uc.ChromeOptions()
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-service-autorun")
        chrome_options.add_argument("--password-store=basic")

        driver = uc.Chrome(options=chrome_options, use_subprocess=True)
    except Exception as e:
        console.print(
            f"[bold red]Error:[/bold red] Could not launch Chrome: {e}\n"
            "Make sure Google Chrome is installed.\n"
            "Tip: close ALL Chrome windows and try again."
        )
        sys.exit(1)

    driver.get("https://x.com/i/flow/login")
    console.print("[dim]Aguardando login...[/dim]", end="")

    ct0, auth_token = None, None
    max_wait = 300  # 5 minutes
    start = time.time()

    while time.time() - start < max_wait:
        try:
            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            ct0 = cookies.get("ct0")
            auth_token = cookies.get("auth_token")

            if ct0 and auth_token:
                break
        except Exception:
            pass  # Browser may be navigating

        time.sleep(1.5)

    try:
        driver.quit()
    except Exception:
        pass

    if not ct0 or not auth_token:
        console.print("\n[bold red]Tempo esgotado ao aguardar login (5 min).[/bold red]")
        sys.exit(1)

    console.print(" [bold green]Login detectado![/bold green]")

    # ── Save cookies ──
    with open(save_path, "w") as f:
        f.write(f"ct0={ct0}\nauth_token={auth_token}\n")
    console.print(f"[dim]Cookies saved to: {save_path.resolve()}[/dim]\n")

    return ct0, auth_token


def load_saved_cookies() -> tuple[str, str] | None:
    """Tries to load previously saved cookies from the default path."""
    if DEFAULT_COOKIES_PATH.exists():
        try:
            ct0, auth_token = load_cookies_from_file(str(DEFAULT_COOKIES_PATH))
            return ct0, auth_token
        except (ValueError, FileNotFoundError):
            pass
    return None


def print_banner():
    banner = Text()
    banner.append("\n  ╔═══════════════════════════════════════╗\n", style="bold cyan")
    banner.append("  ║      ", style="bold cyan")
    banner.append("X  M E D I A  S C R A P E R", style="bold white")
    banner.append("      ║\n", style="bold cyan")
    banner.append("  ║   ", style="bold cyan")
    banner.append("Photos & Videos - Max Quality", style="dim white")
    banner.append("    ║\n", style="bold cyan")
    banner.append("  ╚═══════════════════════════════════════╝\n", style="bold cyan")
    console.print(banner)


def ask(prompt: str, default: str = "") -> str:
    """Simple input prompt with rich formatting."""
    suffix = f" [dim]({default})[/dim]" if default else ""
    console.print(f"  [bold cyan]>[/bold cyan] {prompt}{suffix}: ", end="")
    value = input().strip()
    return value if value else default


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str = "1") -> str:
    """Numbered menu prompt. Returns the value from the selected option tuple."""
    console.print(f"\n  [bold cyan]>[/bold cyan] {prompt}\n")
    for i, (label, _value) in enumerate(options, 1):
        marker = "[bold cyan]*[/bold cyan]" if str(i) == default else " "
        console.print(f"    {marker} [bold]{i}[/bold] - {label}")
    console.print()
    console.print(f"  [dim]Escolha[/dim] [dim]({default})[/dim]: ", end="")
    choice = input().strip() or default
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx][1]
    except ValueError:
        pass
    return options[int(default) - 1][1]


def main():
    print_banner()

    # ══════════════════════════════════════════════════════════════
    # STEP 1: Authentication
    # ══════════════════════════════════════════════════════════════
    ct0, auth_token = None, None
    saved = load_saved_cookies()

    if saved:
        console.print("  [green]Cookies salvos encontrados![/green]")
        use_saved = ask("Usar cookies salvos? (s/n)", "s").lower()
        if use_saved in ("s", "sim", "y", "yes", ""):
            ct0, auth_token = saved
            console.print("  [dim]Usando cookies salvos.[/dim]\n")
        else:
            console.print()
            ct0, auth_token = login_with_selenium()
    else:
        console.print("  [yellow]Nenhum cookie salvo encontrado.[/yellow]")
        console.print("  [dim]Abrindo navegador para login...[/dim]\n")
        ct0, auth_token = login_with_selenium()

    # ══════════════════════════════════════════════════════════════
    # STEP 2: Resolve GraphQL endpoints (once per session)
    # ══════════════════════════════════════════════════════════════
    try:
        endpoints = resolve_graphql_endpoints()
    except RuntimeError as e:
        console.print(f"[bold red]Erro:[/bold red] {e}")
        sys.exit(1)

    session = XSession(ct0, auth_token)

    # ══════════════════════════════════════════════════════════════
    # MAIN LOOP: Download profiles
    # ══════════════════════════════════════════════════════════════
    while True:
        console.print()
        console.rule("[bold cyan]Novo Download[/bold cyan]")
        console.print()

        # ── Ask username ──
        username = ask("Username do perfil (sem @)").lstrip("@")
        if not username:
            console.print("  [red]Username nao pode ser vazio.[/red]")
            continue

        # ── Resolve user ──
        console.print(f"\n  [dim]Buscando @{username}...[/dim]", end="")
        try:
            user_id, display_name = get_user_id(session, endpoints["UserByScreenName"], username)
        except Exception as e:
            console.print(f" [red]Erro![/red]")
            console.print(f"  [red]{e}[/red]")
            continue

        console.print(f" [green]OK[/green] - [bold]{display_name}[/bold]\n")

        # ── Ask media type ──
        media_filter = ask_choice(
            "O que deseja baixar?",
            [
                ("Fotos e Videos (tudo)", "all"),
                ("Apenas Fotos", "photos"),
                ("Apenas Videos", "videos"),
            ],
            default="1",
        )

        # ── Ask threads ──
        console.print()
        threads_str = ask("Quantas threads simultaneas?", "4")
        try:
            max_threads = max(1, min(int(threads_str), 32))
        except ValueError:
            max_threads = 4
            console.print("  [dim]Valor invalido, usando 4 threads.[/dim]")

        # ── Fetch media ──
        console.print(f"\n  [bold]Escaneando timeline de midia de @{username}...[/bold]\n")
        media_items = fetch_all_media(session, endpoints["UserMedia"], user_id, username)

        if not media_items:
            console.print("  [yellow]Nenhuma midia encontrada para este usuario.[/yellow]")
            continue

        # ── Filter ──
        if media_filter == "photos":
            media_items = [m for m in media_items if m["type"] == "photo"]
        elif media_filter == "videos":
            media_items = [m for m in media_items if m["type"] in ("video", "gif")]

        if not media_items:
            console.print(f"  [yellow]Nenhuma midia do tipo selecionado encontrada.[/yellow]")
            continue

        # ── Summary ──
        photos = [m for m in media_items if m["type"] == "photo"]
        videos = [m for m in media_items if m["type"] in ("video", "gif")]
        output_dir = Path("downloads") / username

        console.print()
        table = Table(box=box.ROUNDED, show_header=False, title_style="bold", padding=(0, 2))
        table.add_column("Key", style="dim")
        table.add_column("Value", style="bold")
        table.add_row("Perfil", f"@{username} ({display_name})")
        table.add_row("Fotos", str(len(photos)))
        table.add_row("Videos", str(len(videos)))
        table.add_row("Total", str(len(media_items)))
        table.add_row("Threads", str(max_threads))
        table.add_row("Pasta", str(output_dir.resolve()))
        table.add_row("Estrutura", "Fotos/ + Videos/")
        console.print(table)
        console.print()

        # ── Download ──
        console.print("  [bold]Iniciando downloads...[/bold]\n")
        successes, failures = download_all_media(media_items, output_dir, max_threads)

        # ── Results ──
        console.print()
        if failures:
            console.print(
                Panel(
                    f"[green]{successes} baixados com sucesso[/green]  |  "
                    f"[red]{len(failures)} falharam[/red]",
                    title="Resultado",
                    border_style="yellow",
                )
            )
            if len(failures) <= 10:
                for f in failures:
                    console.print(f"    [red]x[/red] {f}")
        else:
            console.print(
                Panel(
                    f"[bold green]{successes} arquivos baixados com sucesso![/bold green]",
                    title="Resultado",
                    border_style="green",
                )
            )

        console.print(f"  [dim]Fotos salvas em: {(output_dir / 'Fotos').resolve()}[/dim]")
        console.print(f"  [dim]Videos salvos em: {(output_dir / 'Videos').resolve()}[/dim]")

        # ── Continue or exit ──
        console.print()
        again = ask("Baixar outro perfil? (s/n)", "s").lower()
        if again not in ("s", "sim", "y", "yes", ""):
            break

    console.print("\n  [bold cyan]Ate mais![/bold cyan]\n")


if __name__ == "__main__":
    main()

