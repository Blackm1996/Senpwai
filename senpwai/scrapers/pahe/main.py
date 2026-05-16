import re
import os
import json
import math
import importlib.util
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, cast
from requests.cookies import RequestsCookieJar
from requests import Response
from bs4 import BeautifulSoup, Tag
from senpwai.common.scraper import (
    CLIENT,
    PARSER,
    AiringStatus,
    AnimeMetadata,
    DomainNameError,
    ProgressFunction,
    get_new_home_url_from_readme,
    closest_quality_index,
)
from senpwai.scrapers.pahe.constants import (
    CHAR_MAP_BASE,
    CHAR_MAP_DIGITS,
    PAHE_HOME_URL,
    FULL_SITE_NAME,
    API_ENTRY_POINT,
    ANIME_PAGE_URL,
    LOAD_EPISODES_URL,
    DUB_PATTERN,
    EPISODE_PAGE_URL,
    EPISODE_SIZE_REGEX,
    KWIK_PAGE_REGEX,
    PARAM_REGEX,
)

FIRST_REQUEST = True
COOKIES = {"__ddg1_": "", "__ddg2_": ""}
KWIK_SESSION_COOKIES = RequestsCookieJar()
KWIK_SESSION_USER_AGENT = ""


PAHE_DEBUG_LOG_PATH = os.environ.get("SENPWAI_PAHE_DEBUG_LOG", r"D:\Blackm\Documents\senpwai_pahe_debug.log")
PLAYWRIGHT_HEADLESS = os.environ.get("SENPWAI_PLAYWRIGHT_HEADLESS", "1") != "0"
PLAYWRIGHT_MANUAL_WAIT_MS = int(os.environ.get("SENPWAI_PLAYWRIGHT_MANUAL_WAIT_MS", "90000"))
PLAYWRIGHT_HEADLESS_CHALLENGE_TIMEOUT_MS = int(
    os.environ.get("SENPWAI_PLAYWRIGHT_HEADLESS_CHALLENGE_TIMEOUT_MS", "20000")
)


def _pahe_debug(event: str, **data: Any) -> None:
    try:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with open(PAHE_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _cookie_snapshot(jar_or_dict: Any) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    if isinstance(jar_or_dict, dict):
        for name, value in jar_or_dict.items():
            snapshot.append({"name": name, "value_len": len(str(value))})
        return snapshot
    for cookie in jar_or_dict:
        snapshot.append({
            "name": cookie.name,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
            "value_len": len(cookie.value or ""),
        })
    return snapshot


def _playwright_is_available() -> bool:
    try:
        available = importlib.util.find_spec("playwright.sync_api") is not None
        _pahe_debug("playwright_available", available=available)
        return available
    except ModuleNotFoundError:
        return False


def _refresh_pahe_cookies_with_browser(url: str) -> bool:
    """Use a real browser session to refresh Animepahe cookies after manual verification."""
    _pahe_debug("refresh_pahe_cookies_start", url=url)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _pahe_debug("refresh_pahe_cookies_import_error", error=str(exc))
        return False

    cookies: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=PLAYWRIGHT_HEADLESS,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(8000)
            cookies = context.cookies()
            _pahe_debug("refresh_pahe_cookies_browser_captured", count=len(cookies), cookies=cookies)
            browser.close()
    except Exception as exc:
        _pahe_debug("refresh_pahe_cookies_browser_error", url=url, error=str(exc))
        return False

    pahe_cookies = {
        cookie["name"]: cookie["value"]
        for cookie in cookies
        if "animepahe" in cookie.get("domain", "")
    }
    if not pahe_cookies:
        _pahe_debug("refresh_pahe_cookies_empty")
        return False
    COOKIES.update(pahe_cookies)
    _pahe_debug("refresh_pahe_cookies_done", cookies=_cookie_snapshot(COOKIES))
    return True
"""
For some reason these cookies just need to be set as in they don't even need to be valid
If something crashes, try updating to something like: 
COOKIES = {
    "__ddg1_": f"; Expires=Tue, 19 Jan 2038 03:14:07 GMT; Domain={PAHE_DOMAIN}; Path=/",
    "__ddg2_": f"; Expires=Tue, 19 Jan 2038 03:14:07 GMT; Domain={PAHE_DOMAIN}; Path=/",
}
Also it seems currently only __ddg2_ is necessary
"""


def site_request(url: str, allow_redirects=False) -> Response:
    """
    For requests that go specifically to the domain animepahe.ru instead of e.g., pahe.win or kwik.si
    Typically these requests need the cookies
    """
    _pahe_debug("site_request_start", url=url, allow_redirects=allow_redirects, cookies=_cookie_snapshot(COOKIES))
    try:
        # We only want to handle the domain change incase this is the first request
        # This is to avoid raising DomainNameError when the something else broke instead
        global FIRST_REQUEST
        if FIRST_REQUEST:
            FIRST_REQUEST = False
            response = CLIENT.get(
                url,
                cookies=COOKIES,
                allow_redirects=allow_redirects,
                exceptions_to_raise=(DomainNameError, KeyboardInterrupt),
            )
        else:
            response = CLIENT.get(url, cookies=COOKIES, allow_redirects=allow_redirects)
        COOKIES.update(response.cookies)
        _pahe_debug("site_request_response", url=url, status_code=response.status_code, response_url=response.url, set_cookie_count=len(response.cookies))
    except DomainNameError:
        _pahe_debug("site_request_domain_change_detected", url=url)
        global PAHE_HOME_URL
        PAHE_HOME_URL = get_new_home_url_from_readme(FULL_SITE_NAME)
        return site_request(url)
    if response.status_code in (403, 503):
        _pahe_debug("site_request_challenge_status", status_code=response.status_code, url=url)
        refreshed = _refresh_pahe_cookies_with_browser(PAHE_HOME_URL)
        _pahe_debug("site_request_refresh_result", refreshed=refreshed)
        if refreshed:
            response = CLIENT.get(url, cookies=COOKIES, allow_redirects=allow_redirects)
            COOKIES.update(response.cookies)
    return response


def search(keyword: str) -> list[dict[str, str]]:
    search_url = f"{API_ENTRY_POINT}search&q={keyword}"
    response = site_request(search_url)
    results_json = cast(dict, response.json())
    # The search api endpoint won't return json containing the data key if no results are found
    return results_json.get("data", [])


def extract_anime_title_page_link_and_id(
    result: dict[str, str],
) -> tuple[str, str, str]:
    anime_id = result["session"]
    title = result["title"]
    page_link = ANIME_PAGE_URL.format(anime_id)
    return title, page_link, anime_id


class EpisodePagesInfo(NamedTuple):
    start_page_num: int
    end_page_num: int
    total: int
    first_page_json: dict[str, Any]


def get_episode_pages_info(
    anime_page_link: str, start_episode: int, end_episode: int
) -> EpisodePagesInfo:
    page_url = LOAD_EPISODES_URL.format(anime_page_link, 1)
    first_page_json = site_request(page_url).json()
    per_page: int = first_page_json["per_page"]
    start_page_num = math.ceil(start_episode / per_page)
    end_page_num = math.ceil(end_episode / per_page)
    total = (end_page_num - start_page_num) + 1
    return EpisodePagesInfo(
        start_page_num, end_page_num, total, first_page_json
    )


class GetEpisodePageLinks(ProgressFunction):
    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def generate_episode_page_links(
        start_episode: int,
        end_episode: int,
        first_episode: int,
        episodes_data: list[dict[str, Any]],
        anime_id: str,
    ):
        start_idx = 0
        end_idx = len(episodes_data) - 1

        for idx, episode in enumerate(episodes_data):
            # Sometimes for sequels animepahe continues the episode numbers from the last episode of the previous season
            # For instance  "Boku no Hero Academia 2nd Season" episode 1 is shown as episode 14
            # So we do episode - (first_episode - 1) to get the real episode number e.g.,
            # 14 - (14 - 1) = 1
            # 15 - (14 - 1) = 2 and so on
            episode_num = episode["episode"] - (first_episode - 1)
            if episode_num == start_episode:
                start_idx = idx
            if episode_num == end_episode: 
                end_idx = idx
                break
        episodes_data = episodes_data[start_idx : end_idx + 1]
        episode_sessions = [episode["session"] for episode in episodes_data]
        return [
            EPISODE_PAGE_URL.format(anime_id, episode_session)
            for episode_session in episode_sessions
        ]

    # Retrieves a list of the episode page links (not download links)
    def get_episode_page_links(
        self,
        start_episode: int,
        end_episode: int,
        episode_pages_info: EpisodePagesInfo,
        anime_page_link: str,
        anime_id: str,
        progress_update_callback: Callable[[int], None] | None = None,
    ) -> list[str]:
        page_url = anime_page_link
        (
            start_page_num,
            end_page_num,
            _,
            first_page_json,
        ) = episode_pages_info

        episodes_data: list[dict[str, Any]] = []
        if start_page_num == 1:
            episodes_data.extend(first_page_json["data"])
            start_page_num += 1
            if progress_update_callback:
                progress_update_callback(1)
        for page_num in range(start_page_num, end_page_num + 1):
            page_url = LOAD_EPISODES_URL.format(anime_page_link, page_num)
            page_json = site_request(page_url).json()
            # To avoid episodes like 7.5 and 5.5 cause they're usually just recaps
            episodes = [
                ep for ep in page_json["data"] if isinstance(ep["episode"], int)
            ]
            episodes_data.extend(episodes)
            page_url = page_json["next_page_url"]
            self.resume.wait()
            if self.cancelled:
                return []
            if progress_update_callback:
                progress_update_callback(1)
        first_episode_json = next(
            ep for ep in first_page_json["data"] if isinstance(ep["episode"], int)
        )
        first_episode = first_episode_json["episode"]
        return GetEpisodePageLinks.generate_episode_page_links(
            start_episode,
            end_episode,
            first_episode,
            episodes_data,
            anime_id,
        )


class GetPahewinPageLinks(ProgressFunction):
    def __init__(self) -> None:
        super().__init__()

    def get_pahewin_page_links_and_info(
        self,
        episode_page_links: list[str],
        progress_update_callback: Callable[[int], None] | None = None,
    ) -> tuple[list[list[str]], list[list[str]]]:
        pahewin_links: list[list[str]] = []
        download_info: list[list[str]] = []
        for episode_page_link in episode_page_links:
            page_content = site_request(episode_page_link, allow_redirects=True).content
            soup = BeautifulSoup(page_content, PARSER)
            pahewin_data = soup.find_all("a", class_="dropdown-item", target="_blank")
            if pahewin_data:
                pahewin_links.append([cast(str, link["href"]) for link in pahewin_data])
                download_info.append([li.text.strip() for li in pahewin_data])
            self.resume.wait()
            if self.cancelled:
                return ([], [])
            if progress_update_callback:
                progress_update_callback(1)
        return (pahewin_links, download_info)


def is_dub(episode_download_info: str) -> bool:
    return episode_download_info.endswith(DUB_PATTERN)


def dub_available(anime_page_link: str, anime_id: str) -> bool:
    page_url = LOAD_EPISODES_URL.format(anime_page_link, 1)
    page_json = site_request(page_url).json()
    episodes_data = page_json.get("data", None)
    if episodes_data is None:
        return False
    episode_sessions = [episode["session"] for episode in episodes_data]
    episode_page_link = EPISODE_PAGE_URL.format(anime_id, episode_sessions[0])
    (
        _,
        download_info,
    ) = GetPahewinPageLinks().get_pahewin_page_links_and_info([episode_page_link])

    for info in download_info[0]:
        if is_dub(info):
            return True
    return False


def bind_sub_or_dub_to_link_info(
    sub_or_dub: str,
    pahewin_download_page_links: list[list[str]],
    download_info: list[list[str]],
) -> tuple[list[list[str]], list[list[str]]]:
    bound_links: list[list[str]] = []
    bound_info: list[list[str]] = []
    for link_list, episode_info in zip(pahewin_download_page_links, download_info):
        links: list[str] = []
        infos: list[str] = []
        for link, info in zip(link_list, episode_info):
            is_dub_link = is_dub(info)
            if (sub_or_dub == "sub" and not is_dub_link) or (
                sub_or_dub == "dub" and is_dub_link
            ):
                links.append(link)
                infos.append(info)
        if links and infos:
            bound_links.append(links)
            bound_info.append(infos)
    return (bound_links, bound_info)


def bind_quality_to_link_info(
    quality: str,
    pahewin_download_page_links: list[list[str]],
    download_info: list[list[str]],
) -> tuple[list[str], list[str]]:
    bound_links: list[str] = []
    bound_info: list[str] = []
    for links, infos in zip(pahewin_download_page_links, download_info):
        index = closest_quality_index(infos, quality)
        bound_links.append(links[index])
        bound_info.append(infos[index])
    return (bound_links, bound_info)


def calculate_total_download_size(bound_info: list[str]) -> int:
    total_size = 0
    for episode in bound_info:
        match = cast(re.Match, EPISODE_SIZE_REGEX.search(episode))
        size = int(match.group(1))
        total_size += size
    return total_size 


def get_char_code(content: str, s1: int) -> int:
    j = 0
    for index, c in enumerate(reversed(content)):
        j += (int(c) if c.isdigit() else 0) * int(math.pow(s1, index))
    k = ""
    while j > 0:
        k = CHAR_MAP_DIGITS[j % CHAR_MAP_BASE] + k
        j = (j - (j % CHAR_MAP_BASE)) // CHAR_MAP_BASE
    return int(k) if k else 0


# Courtesy of Saikou app https://github.com/saikou-app/saikou
# RIP Saikou
def decrypt_post_form(full_key: str, key: str, v1: int, v2: int) -> str:
    r = ""
    i = 0
    while i < len(full_key):
        s = ""
        while full_key[i] != key[v2]:
            s += full_key[i]
            i += 1
        for idx, c in enumerate(key):
            s = s.replace(c, str(idx))
        r += chr(get_char_code(s, v2) - v1)
        i += 1
    return r




def _resolve_direct_links_with_browser(kwik_page_links: list[str]) -> dict[str, str]:
    _pahe_debug("browser_resolve_start", kwik_page_links=kwik_page_links)
    """Resolve Kwik links using one persistent browser session and network capture."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _pahe_debug("browser_resolve_import_error", error=str(exc))
        return {}

    def _probe_page_state(page) -> dict[str, Any]:
        try:
            state = page.evaluate(
                """
                () => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    const title = (document.title || '').toLowerCase();
                    const challengeIframes = document.querySelectorAll("iframe[src*='challenges.cloudflare.com'], iframe[title*='challenge'], iframe[src*='turnstile']").length;
                    const hasSubmit = !!document.querySelector("form button[type='submit'], button[type='submit'], form input[type='submit'], a#downloadButton");
                    const hasForm = !!document.querySelector("form");
                    const hasChallengeText = ["just a moment", "verify you are human", "checking your browser", "cf-challenge", "cloudflare"]
                        .some((i) => text.includes(i) || title.includes(i));
                    return { challengeIframes, hasSubmit, hasForm, hasChallengeText, bodyTextLength: text.length, title };
                }
                """
            )
            return cast(dict[str, Any], state)
        except Exception as exc:
            return {"probe_error": str(exc)}

    def pick_best_candidate(candidates: list[str]) -> str | None:
        if not candidates:
            return None

        def score(url: str) -> int:
            u = url.lower()
            points = 0
            if "animepahe" in u or "subsplease" in u or "file=" in u:
                points += 100
            if "owocdn" in u or "vault-" in u:
                points += 40
            if "cdn.nightdestruct.com" in u or "/sb/notifications/" in u:
                points -= 200
            if "kwik.cx/d/" in u:
                points -= 20
            if ".mp4" in u:
                points += 10
            return points

        ranked = sorted(candidates, key=score, reverse=True)
        _pahe_debug("browser_candidate_ranked", ranked=ranked)
        return ranked[0]

    def pick_candidate_with_length(candidates: list[str], referer_url: str) -> str | None:
        ranked = sorted(candidates, key=lambda u: (u == pick_best_candidate(candidates)), reverse=True)
        for candidate in ranked:
            try:
                response = CLIENT.get(
                    candidate,
                    headers=CLIENT.make_headers({"Referer": referer_url}),
                    cookies=get_kwik_session_cookies(),
                    allow_redirects=True,
                    stream=True,
                )
                content_length = response.headers.get("Content-Length")
                _pahe_debug(
                    "browser_candidate_length_probe",
                    candidate=candidate,
                    final_url=response.url,
                    status_code=response.status_code,
                    content_length=content_length,
                )
                if content_length and str(content_length).isdigit() and int(content_length) > 0:
                    response.close()
                    return response.url or candidate
                response.close()
            except Exception as exc:
                _pahe_debug("browser_candidate_length_probe_error", candidate=candidate, error=str(exc))
        return pick_best_candidate(candidates)

    def wait_for_challenge_to_clear(page, timeout_ms: int = 120000) -> bool:
        elapsed = 0
        step_ms = 2000
        while elapsed < timeout_ms:
            page.wait_for_timeout(step_ms)
            elapsed += step_ms
            probe = _probe_page_state(page)
            page_url = page.url
            success = bool(
                (
                    "/f/" in page_url
                    and (probe.get("hasForm") or probe.get("hasSubmit"))
                    and not probe.get("hasChallengeText")
                )
                or ("/d/" in page_url and not probe.get("hasChallengeText"))
            )
            _pahe_debug(
                "browser_challenge_probe",
                elapsed_ms=elapsed,
                page_url=page_url,
                probe=probe,
                success=success,
            )
            if success:
                return True
        return False

    if not kwik_page_links:
        return {}

    def _run_browser_resolution(headless: bool) -> tuple[list[dict[str, Any]], str]:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            # Warm up challenge/session once on the first link, then reuse same context.
            warmup_link = kwik_page_links[0]
            page.goto(warmup_link, wait_until="domcontentloaded")
            warmup_timeout_ms = (
                PLAYWRIGHT_HEADLESS_CHALLENGE_TIMEOUT_MS if headless else 120000
            )
            warmup_cleared = wait_for_challenge_to_clear(page, timeout_ms=warmup_timeout_ms)
            _pahe_debug(
                "browser_warmup_probe_done",
                warmup_link=warmup_link,
                headless=headless,
                timeout_ms=warmup_timeout_ms,
                cleared=warmup_cleared,
            )
            if not warmup_cleared and not headless:
                _pahe_debug("browser_manual_wait_start", wait_ms=PLAYWRIGHT_MANUAL_WAIT_MS, warmup_link=warmup_link)
                page.wait_for_timeout(PLAYWRIGHT_MANUAL_WAIT_MS)
                warmup_cleared = wait_for_challenge_to_clear(page)
            if not warmup_cleared:
                probe = _probe_page_state(page)
                _pahe_debug("browser_warmup_failed", warmup_link=warmup_link, final_url=page.url, probe=probe, headless=headless)
                browser.close()
                return [], ""

            _pahe_debug(
                "browser_session_warmed",
                warmup_link=warmup_link,
                final_url=page.url,
                headless=headless,
                link_count=len(kwik_page_links),
            )

            cookies = context.cookies()
            user_agent = page.evaluate("() => navigator.userAgent")
            _pahe_debug("browser_context_cookies", count=len(cookies), cookies=cookies, headless=headless)
            browser.close()
        return cookies, user_agent

    cookies, user_agent = _run_browser_resolution(PLAYWRIGHT_HEADLESS)
    if not cookies and PLAYWRIGHT_HEADLESS:
        _pahe_debug("browser_headless_retry_headed")
        cookies, user_agent = _run_browser_resolution(False)
    global KWIK_SESSION_COOKIES
    global KWIK_SESSION_USER_AGENT
    KWIK_SESSION_COOKIES = RequestsCookieJar()
    KWIK_SESSION_USER_AGENT = user_agent if isinstance(user_agent, str) else ""

    for cookie in cookies:
        domain = cookie.get("domain", "")
        if "kwik" not in domain and "pahe" not in domain:
            continue
        KWIK_SESSION_COOKIES.set(
            cookie["name"],
            cookie["value"],
            domain=domain,
            path=cookie.get("path", "/"),
            secure=bool(cookie.get("secure", False)),
        )
    _pahe_debug(
        "browser_session_ready",
        kwik_session_cookies=_cookie_snapshot(KWIK_SESSION_COOKIES),
        user_agent=KWIK_SESSION_USER_AGENT,
    )
    return {}


def get_kwik_session_cookies() -> RequestsCookieJar:
    return KWIK_SESSION_COOKIES.copy()


def _kwik_session_is_warmed() -> bool:
    names = {cookie.name for cookie in KWIK_SESSION_COOKIES}
    return bool(names.intersection({"cf_clearance", "kwik_session", "srv"}))


def _retry_kwik_links_with_session(kwik_page_links: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    kwik_cookies = get_kwik_session_cookies()
    extra_headers = (
        {"User-Agent": KWIK_SESSION_USER_AGENT} if KWIK_SESSION_USER_AGENT else {}
    )
    for kwik_page_link in kwik_page_links:
        response = CLIENT.get(
            kwik_page_link,
            cookies=kwik_cookies,
            headers=CLIENT.make_headers(extra_headers),
        )
        _pahe_debug(
            "session_retry_get",
            kwik_page_link=kwik_page_link,
            status_code=response.status_code,
            response_url=response.url,
            text_prefix=response.text[:180],
            using_browser_ua=bool(KWIK_SESSION_USER_AGENT),
        )
        match = PARAM_REGEX.search(response.text)
        if not match:
            _pahe_debug(
                "session_retry_param_regex_miss",
                kwik_page_link=kwik_page_link,
                status_code=response.status_code,
            )
            continue
        full_key, key, v1, v2 = match.group(1), match.group(2), match.group(3), match.group(4)
        form = decrypt_post_form(full_key, key, int(v1), int(v2))
        soup = BeautifulSoup(form, PARSER)
        post_url = cast(str, cast(Tag, soup.form)["action"])
        token_value = cast(str, cast(Tag, soup.input)["value"])
        post_response = CLIENT.post(
            post_url,
            headers=CLIENT.make_headers({"Referer": kwik_page_link, **extra_headers}),
            cookies=kwik_cookies,
            data={"_token": token_value},
            allow_redirects=False,
        )
        direct_download_link = post_response.headers.get("Location")
        _pahe_debug(
            "session_retry_post",
            kwik_page_link=kwik_page_link,
            post_url=post_url,
            status_code=post_response.status_code,
            location=direct_download_link,
        )
        if direct_download_link:
            resolved[kwik_page_link] = direct_download_link
    return resolved


def _upgrade_kwik_download_url(download_url: str, referer_url: str) -> str:
    if "kwik.cx/d/" not in download_url:
        return download_url
    try:
        kwik_cookies = get_kwik_session_cookies()
        response = CLIENT.get(
            download_url,
            headers=CLIENT.make_headers({"Referer": referer_url}),
            cookies=kwik_cookies,
            allow_redirects=False,
        )
        location = response.headers.get("Location")
        _pahe_debug(
            "kwik_download_upgrade_attempt",
            referer_url=referer_url,
            download_url=download_url,
            status_code=response.status_code,
            location=location,
        )
        if location and location.startswith("http"):
            return location
        if response.status_code == 405:
            follow_response = CLIENT.get(
                download_url,
                headers=CLIENT.make_headers({"Referer": referer_url}),
                cookies=kwik_cookies,
                allow_redirects=True,
            )
            _pahe_debug(
                "kwik_download_upgrade_follow",
                referer_url=referer_url,
                download_url=download_url,
                final_url=follow_response.url,
                status_code=follow_response.status_code,
            )
            if follow_response.url and follow_response.url != download_url:
                return follow_response.url
    except Exception as exc:
        _pahe_debug(
            "kwik_download_upgrade_error",
            referer_url=referer_url,
            download_url=download_url,
            error=str(exc),
        )
    return download_url


class GetDirectDownloadLinks(ProgressFunction):
    def __init__(self) -> None:
        super().__init__()

    def get_direct_download_links(
        self,
        pahewin_download_page_links: list[str],
        progress_update_callback: Callable[[int], None] | None = None,
    ) -> list[str]:
        direct_download_links: list[str] = []
        refreshed = _refresh_pahe_cookies_with_browser(PAHE_HOME_URL)
        _pahe_debug("refresh_pahe_cookies_initial_result", refreshed=refreshed)
        unresolved_kwik_links: list[str] = []
        unresolved_progress_pending = 0
        for pahewin_link in pahewin_download_page_links:
            # Extract kwik page links
            pahewin_html_page = CLIENT.get(pahewin_link).text
            kwik_match = KWIK_PAGE_REGEX.search(pahewin_html_page)
            if not kwik_match:
                self.resume.wait()
                if self.cancelled:
                    return []
                if progress_update_callback:
                    progress_update_callback(1)
                continue
            kwik_page_link = kwik_match.group()

            # Extract direct download links from kwik html page
            response = CLIENT.get(kwik_page_link)
            match = PARAM_REGEX.search(response.text)
            if not match:
                _pahe_debug("kwik_param_regex_miss", kwik_page_link=kwik_page_link, status_code=response.status_code, response_url=response.url, text_prefix=response.text[:300])
                unresolved_kwik_links.append(kwik_page_link)
                unresolved_progress_pending += 1
                continue
            full_key, key, v1, v2 = match.group(1), match.group(2), match.group(3), match.group(4)
            form = decrypt_post_form(full_key, key, int(v1), int(v2))
            soup = BeautifulSoup(form, PARSER)
            post_url = cast(str, cast(Tag, soup.form)["action"])
            token_value = cast(str, cast(Tag, soup.input)["value"])
            response = CLIENT.post(
                post_url,
                headers=CLIENT.make_headers({"Referer": kwik_page_link}),
                cookies=response.cookies,
                data={"_token": token_value},
                allow_redirects=False,
            )
            direct_download_link = response.headers.get("Location")
            if not direct_download_link:
                _pahe_debug("kwik_post_no_location", kwik_page_link=kwik_page_link, status_code=response.status_code, headers=dict(response.headers))
                unresolved_kwik_links.append(kwik_page_link)
                unresolved_progress_pending += 1
            else:
                if "kwik.cx/" in direct_download_link:
                    _pahe_debug(
                        "kwik_direct_link_intermediate",
                        kwik_page_link=kwik_page_link,
                        direct_link=direct_download_link,
                    )
                    unresolved_kwik_links.append(kwik_page_link)
                    unresolved_progress_pending += 1
                else:
                    _pahe_debug("kwik_direct_link_resolved_normal", kwik_page_link=kwik_page_link, direct_link=direct_download_link)
                    direct_download_links.append(direct_download_link)
                    self.resume.wait()
                    if self.cancelled:
                        return []
                    if progress_update_callback:
                        progress_update_callback(1)
        if unresolved_kwik_links and _playwright_is_available():
            # Warm challenge/session once in browser, then resolve all episodes via HTTP using that session.
            _resolve_direct_links_with_browser(unresolved_kwik_links)
            warmed = _kwik_session_is_warmed()
            session_resolved: dict[str, str] = {}
            if warmed:
                _pahe_debug("fallback_session_retry_start", retry_count=len(unresolved_kwik_links))
                session_resolved = _retry_kwik_links_with_session(unresolved_kwik_links)
            browser_resolved: dict[str, str] = session_resolved
            unresolved_after_browser = [
                link for link in unresolved_kwik_links if link not in browser_resolved
            ]
            _pahe_debug(
                "fallback_resolution_summary",
                unresolved_count=len(unresolved_kwik_links),
                browser_direct_count=0,
                final_resolved_count=len(browser_resolved),
                warmed=warmed,
                unresolved_after_browser=unresolved_after_browser,
            )
            resolved_count = 0
            intermediate_links: list[str] = []
            for link in unresolved_kwik_links:
                if link not in browser_resolved:
                    continue
                upgraded = _upgrade_kwik_download_url(browser_resolved[link], link)
                if "kwik.cx/d/" in upgraded:
                    _pahe_debug("fallback_unverified_download_url", kwik_page_link=link, resolved_url=upgraded)
                    intermediate_links.append(upgraded)
                    continue
                direct_download_links.append(upgraded)
                resolved_count += 1
            if resolved_count == 0 and intermediate_links:
                _pahe_debug(
                    "fallback_accept_intermediate_urls",
                    count=len(intermediate_links),
                    urls=intermediate_links,
                )
                direct_download_links.extend(intermediate_links)
                resolved_count = len(intermediate_links)
            for _ in range(resolved_count):
                self.resume.wait()
                if self.cancelled:
                    return []
                if progress_update_callback:
                    progress_update_callback(1)
            unresolved_progress_pending -= resolved_count
        # For links that remained unresolved even after browser fallback, still move progress.
        for _ in range(max(unresolved_progress_pending, 0)):
            self.resume.wait()
            if self.cancelled:
                return []
            if progress_update_callback:
                progress_update_callback(1)
        return direct_download_links


def get_anime_metadata(anime_id: str) -> AnimeMetadata:
    page_link = f"{PAHE_HOME_URL}/anime/{anime_id}"
    page_content = site_request(page_link, allow_redirects=True).content
    soup = BeautifulSoup(page_content, PARSER)
    poster = soup.find(class_="youtube-preview")
    if not isinstance(poster, Tag):
        poster = cast(Tag, soup.find(class_="poster-image"))
    poster_url = cast(str, poster["href"])
    summary = cast(Tag, soup.find(class_="anime-synopsis")).get_text()
    genres_tag = cast(Tag, soup.find(class_="anime-genre font-weight-bold"))
    genres = (
        [
            cast(str, cast(Tag, genre_tag.find("a")["title"]))
            for genre_tag in genres_tag.find_all("li")
        ]
        if genres_tag
        else []
    )
    season_and_year = cast(
        str, cast(Tag, soup.select_one('a[href*="/anime/season/"]'))["title"]
    )
    _, release_year = season_and_year.split(" ")
    page_link = ANIME_PAGE_URL.format(anime_id)
    page_json = site_request(page_link).json()
    episode_count = page_json["total"]
    tag = soup.find(title="Currently Airing")
    if tag:
        airing_status = AiringStatus.ONGOING
    elif episode_count == 0:
        airing_status = AiringStatus.UPCOMING
    else:
        airing_status = AiringStatus.FINISHED
    return AnimeMetadata(
        poster_url, summary, episode_count, airing_status, genres, int(release_year)
    )
