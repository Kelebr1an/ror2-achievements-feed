"""Publish a validated Steam achievement snapshot using only the standard library."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


APP_ID = 632360
VANITY = "Kelebr1an"
PROFILE = f"https://steamcommunity.com/id/{VANITY}/"
API_BASE = "https://api.steampowered.com"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data" / "achievements.json"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class FeedError(Exception):
    """A diagnostic safe to print: never include response bodies or request URLs."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The API key is a query parameter. Never forward it to a redirect target.
        return None


class SteamClient:
    def __init__(self, api_key, *, opener=None, sleep=time.sleep, attempts=3):
        if not isinstance(api_key, str) or not api_key.strip():
            raise FeedError("STEAM_API_KEY is missing or empty.")
        self._api_key = api_key.strip()
        self._opener = opener if opener is not None else build_opener(NoRedirects())
        self._sleep = sleep
        self._attempts = attempts

    def get(self, endpoint, parameters):
        query = urlencode({"key": self._api_key, **parameters})
        request = Request(
            f"{API_BASE}/{endpoint}/?{query}",
            headers={"Accept": "application/json", "User-Agent": "ror2-achievements-feed/1.0"},
        )
        for attempt in range(self._attempts):
            try:
                with self._opener.open(request, timeout=20) as response:
                    status = response.getcode()
                    if status != 200:
                        raise FeedError("Steam returned an unexpected HTTP status.")
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except HTTPError as error:
                status = error.code
                error.close()
                retry = status == 429 or 500 <= status <= 599
                if not retry or attempt + 1 == self._attempts:
                    raise FeedError(f"Steam request failed (HTTP {status}).") from None
            except (URLError, OSError, TimeoutError):
                if attempt + 1 == self._attempts:
                    raise FeedError("Steam network request failed or timed out.") from None
            if attempt + 1 < self._attempts:
                self._sleep(2**attempt)
        else:
            raise FeedError("Steam request could not be completed.")

        if len(body) > MAX_RESPONSE_BYTES:
            raise FeedError("Steam response exceeds the allowed size.")
        try:
            result = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise FeedError("Steam returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise FeedError("Steam returned an unexpected response structure.")
        return result


def _object(value, label):
    if not isinstance(value, dict):
        raise FeedError(f"Steam response is missing a valid {label} object.")
    return value


def _text(value, label, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise FeedError(f"Steam response is missing a valid {label} string.")
    return value


def resolve_steam_id(client):
    payload = client.get("ISteamUser/ResolveVanityURL/v1", {"vanityurl": VANITY, "url_type": 1})
    response = _object(payload.get("response"), "vanity response")
    if type(response.get("success")) is not int or response["success"] != 1:
        raise FeedError("Steam could not resolve the profile vanity URL.")
    steam_id = response.get("steamid")
    if (
        not isinstance(steam_id, str)
        or not steam_id.isascii()
        or not steam_id.isdigit()
        or len(steam_id) != 17
        or not 0 < int(steam_id) < 2**64
    ):
        raise FeedError("Steam returned an invalid SteamID.")
    return steam_id


def collect_snapshot(client):
    steam_id = resolve_steam_id(client)
    payload = client.get(
        "ISteamUserStats/GetPlayerAchievements/v1",
        {"steamid": steam_id, "appid": APP_ID, "l": "english"},
    )
    stats = _object(payload.get("playerstats"), "playerstats")
    if stats.get("success") is not True:
        raise FeedError("Steam achievements are unavailable. Check profile and game-details privacy.")
    # GetPlayerAchievements normally returns gameName, not appid; the request
    # already fixes AppID. Reject a contradictory ID if Steam includes one.
    if "appid" in stats and (type(stats["appid"]) is not int or stats["appid"] != APP_ID):
        raise FeedError("Steam returned achievements for an unexpected game.")
    if stats.get("steamID") != steam_id:
        raise FeedError("Steam returned achievements for an unexpected player.")
    entries = stats.get("achievements")
    if not isinstance(entries, list) or not entries:
        raise FeedError("Steam returned an empty or incomplete achievement list.")

    achievements = []
    seen = set()
    for entry in entries:
        entry = _object(entry, "achievement")
        apiname = _text(entry.get("apiname"), "apiname")
        if apiname in seen:
            raise FeedError("Steam returned duplicate achievement identifiers.")
        seen.add(apiname)
        achieved = entry.get("achieved")
        unlocktime = entry.get("unlocktime")
        if type(achieved) is not int or achieved not in (0, 1):
            raise FeedError("Steam returned an invalid achievement state.")
        if type(unlocktime) is not int or unlocktime < 0:
            raise FeedError("Steam returned an invalid achievement unlock timestamp.")
        achievements.append(
            {
                "apiname": apiname,
                "name": _text(entry.get("name"), "achievement name"),
                # Steam can intentionally omit the text of hidden achievements.
                "description": _text(entry.get("description"), "description", allow_empty=True),
                "achieved": achieved,
                "unlocktime": unlocktime,
            }
        )
    achievements.sort(key=lambda entry: entry["apiname"])
    unlocked = sum(entry["achieved"] for entry in achievements)
    return {
        "appid": APP_ID,
        "profile": PROFILE,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "total": len(achievements),
        "unlocked": unlocked,
        "remaining": len(achievements) - unlocked,
        "achievements": achievements,
    }


def preserve_if_incomplete(output, snapshot):
    """Reject missing API names; permit newly added achievements and changed unlocks."""
    if not output.exists():
        return
    try:
        previous = json.loads(output.read_text(encoding="utf-8"))
        if previous["appid"] != APP_ID or previous["profile"] != PROFILE:
            raise ValueError
        previous_entries = previous["achievements"]
        if not isinstance(previous_entries, list):
            raise ValueError
        previous_names = {entry["apiname"] for entry in previous_entries}
        if any(not isinstance(name, str) for name in previous_names):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise FeedError("Existing snapshot is invalid; refusing to replace it automatically.") from None
    current_names = {entry["apiname"] for entry in snapshot["achievements"]}
    if not previous_names <= current_names:
        raise FeedError("Steam response is missing previously published achievements; snapshot preserved.")


def atomic_write(output, content):
    temporary = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except OSError:
        raise FeedError("Could not atomically save the snapshot; previous file was preserved.") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def update_feed(output, api_key, *, client=None):
    client = client if client is not None else SteamClient(api_key)
    snapshot = collect_snapshot(client)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if api_key and api_key.strip() in content:
        raise FeedError("Refusing to publish a response containing sensitive data.")
    output = Path(output)
    preserve_if_incomplete(output, snapshot)
    atomic_write(output, content)
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        snapshot = update_feed(args.output, os.environ.get("STEAM_API_KEY", ""))
    except FeedError as error:
        print(f"Update failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Tracebacks may contain request objects or exception URLs with the API key.
        print("Update failed unexpectedly; no diagnostic secrets were printed.", file=sys.stderr)
        return 1
    print(f"Updated Steam achievements: {snapshot['unlocked']}/{snapshot['total']} unlocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
