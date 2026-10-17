from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import fetch_achievements as feed


KEY = "test-secret-not-a-real-steam-key"
STEAM_ID = "76561198836560692"


def achievement(apiname="one", achieved=1, name="Первое"):
    return {"apiname": apiname, "achieved": achieved, "unlocktime": 123 if achieved else 0,
            "name": name, "description": "Описание"}


def payload(entries=None):
    return {"playerstats": {"success": True, "appid": feed.APP_ID, "steamID": STEAM_ID,
                            "achievements": entries if entries is not None else
                            [achievement(), achievement("two", 0)]}}


def client_for(data=None):
    client = Mock()
    client.get.side_effect = [{"response": {"success": 1, "steamid": STEAM_ID}},
                              data if data is not None else payload()]
    return client


class Response(io.BytesIO):
    def getcode(self):
        return 200


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "data" / "achievements.json"

    def write(self, data=None):
        return feed.update_feed(self.output, KEY, client=client_for(data))

    def test_complete_public_contract_and_unicode(self):
        snapshot = self.write()
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(saved, snapshot)
        self.assertEqual(set(saved), {"appid", "profile", "fetched_at", "total", "unlocked",
                                      "remaining", "achievements"})
        self.assertEqual((saved["total"], saved["unlocked"], saved["remaining"]), (2, 1, 1))
        self.assertEqual(set(saved["achievements"][0]),
                         {"apiname", "name", "description", "achieved", "unlocktime"})
        self.assertIn("Первое", self.output.read_text(encoding="utf-8"))
        self.assertIsNotNone(datetime.fromisoformat(saved["fetched_at"].replace("Z", "+00:00")).tzinfo)
        self.assertNotIn(KEY, self.output.read_text(encoding="utf-8"))

    def test_both_api_methods_and_language(self):
        client = client_for()
        feed.collect_snapshot(client)
        self.assertEqual(client.get.call_args_list[0].args,
                         ("ISteamUser/ResolveVanityURL/v1", {"vanityurl": "Kelebr1an", "url_type": 1}))
        self.assertEqual(client.get.call_args_list[1].args,
                         ("ISteamUserStats/GetPlayerAchievements/v1",
                          {"steamid": STEAM_ID, "appid": 632360, "l": "english"}))

    def test_normal_steam_response_without_appid(self):
        data = payload()
        del data["playerstats"]["appid"]
        data["playerstats"]["gameName"] = "Risk of Rain 2"
        self.assertEqual(self.write(data)["appid"], feed.APP_ID)

    def test_dynamic_count_and_new_achievements(self):
        self.write()
        result = self.write(payload([achievement(), achievement("two", 1), achievement("new", 0)]))
        self.assertEqual((result["total"], result["unlocked"], result["remaining"]), (3, 2, 1))

    def test_missing_previously_published_name_preserves_bytes(self):
        self.write()
        before = self.output.read_bytes()
        # Same count still does not justify silently dropping a known identifier.
        with self.assertRaises(feed.FeedError):
            self.write(payload([achievement(), achievement("replacement", 0)]))
        self.assertEqual(before, self.output.read_bytes())

    def test_malformed_or_private_response_preserves_file(self):
        self.write()
        before = self.output.read_bytes()
        bad = [{}, {"playerstats": {"success": False, "error": KEY}},
               payload([]), payload([achievement(), achievement()])]
        for field, value in [("appid", 1), ("steamID", "wrong"), ("success", "true")]:
            item = payload()
            item["playerstats"][field] = value
            bad.append(item)
        for field, value in [("apiname", ""), ("name", None), ("description", None),
                             ("achieved", True), ("achieved", 2), ("unlocktime", -1),
                             ("unlocktime", True)]:
            item = payload()
            item["playerstats"]["achievements"][0][field] = value
            bad.append(item)
        for item in bad:
            with self.subTest(item=item), self.assertRaises(feed.FeedError):
                self.write(item)
            self.assertEqual(before, self.output.read_bytes())

    def test_empty_hidden_description_is_valid(self):
        data = payload()
        data["playerstats"]["achievements"][0]["description"] = ""
        self.assertEqual(self.write(data)["achievements"][0]["description"], "")

    def test_vanity_failure_and_invalid_ids(self):
        for response in [{"success": 42}, {"success": 1, "steamid": 123},
                         {"success": True, "steamid": STEAM_ID},
                         {"success": 1, "steamid": "not-a-steam-id"}]:
            client = Mock()
            client.get.return_value = {"response": response}
            with self.subTest(response=response), self.assertRaises(feed.FeedError):
                feed.resolve_steam_id(client)
            self.assertFalse(self.output.exists())

    def test_atomic_replace_failure_keeps_previous_file_and_removes_temp(self):
        self.write()
        before = self.output.read_bytes()
        with patch.object(feed.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(feed.FeedError):
                self.write()
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(list(self.output.parent.glob("*.tmp")), [])

    def test_invalid_previous_json_is_preserved(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"broken-json")
        with self.assertRaises(feed.FeedError):
            self.write()
        self.assertEqual(self.output.read_bytes(), b"broken-json")

    def test_sensitive_data_is_never_written(self):
        self.write()
        before = self.output.read_bytes()
        data = payload()
        data["playerstats"]["achievements"][0]["description"] = KEY
        with self.assertRaises(feed.FeedError):
            self.write(data)
        self.assertEqual(before, self.output.read_bytes())

    def test_cli_network_failure_preserves_published_snapshot(self):
        self.write()
        before = self.output.read_bytes()
        opener = Mock()
        resolved = {"response": {"success": 1, "steamid": STEAM_ID}}
        opener.open.side_effect = [Response(json.dumps(resolved).encode("utf-8")),
                                   URLError(KEY), URLError(KEY), URLError(KEY)]
        client = feed.SteamClient(KEY, opener=opener, sleep=Mock())
        stderr = io.StringIO()
        with patch.dict(feed.os.environ, {"STEAM_API_KEY": KEY}), patch("sys.stderr", stderr):
            with patch.object(feed, "SteamClient", return_value=client):
                self.assertEqual(feed.main(["--output", str(self.output)]), 1)
        self.assertEqual(before, self.output.read_bytes())
        self.assertNotIn(KEY, stderr.getvalue())
        self.assertEqual(opener.open.call_count, 4)

    def test_missing_key_and_safe_cli_error(self):
        for key in ("", "  "):
            with self.subTest(key=key), self.assertRaises(feed.FeedError):
                feed.SteamClient(key)
        stderr = io.StringIO()
        with patch.dict(feed.os.environ, {}, clear=True), patch("sys.stderr", stderr):
            self.assertEqual(feed.main(["--output", str(self.output)]), 1)
        self.assertIn("STEAM_API_KEY", stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_unexpected_exception_does_not_leak_key_or_traceback(self):
        stderr = io.StringIO()
        with patch.dict(feed.os.environ, {"STEAM_API_KEY": KEY}), patch("sys.stderr", stderr):
            with patch.object(feed, "update_feed", side_effect=RuntimeError(KEY)):
                self.assertEqual(feed.main([]), 1)
        self.assertNotIn(KEY, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class HttpTests(unittest.TestCase):
    def make_client(self, effects):
        opener = Mock()
        opener.open.side_effect = effects
        sleep = Mock()
        return feed.SteamClient(KEY, opener=opener, sleep=sleep), opener, sleep

    def test_request_uses_https_and_timeout(self):
        client, opener, _ = self.make_client([Response(b'{"response": {}}')])
        client.get("ISteamUser/ResolveVanityURL/v1", {"vanityurl": "Kelebr1an"})
        request = opener.open.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual((parsed.scheme, parsed.netloc), ("https", "api.steampowered.com"))
        self.assertEqual(parse_qs(parsed.query)["key"], [KEY])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_transient_http_failure_retries(self):
        error = HTTPError("https://example.invalid/?key=" + KEY, 503, KEY, {}, None)
        client, opener, sleep = self.make_client([error, Response(b'{"ok":true}')])
        self.assertEqual(client.get("test/v1", {}), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_network_retry_exhaustion_is_bounded_and_safe(self):
        client, opener, sleep = self.make_client([URLError(KEY)] * 3)
        with self.assertRaises(feed.FeedError) as caught:
            client.get("test/v1", {})
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_rate_limit_retries(self):
        error = HTTPError("https://example.invalid/?key=" + KEY, 429, KEY, {}, None)
        client, opener, _ = self.make_client([error, Response(b'{"ok":true}')])
        self.assertEqual(client.get("test/v1", {}), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)

    def test_authentication_error_not_retried_or_logged(self):
        error = HTTPError("https://example.invalid/?key=" + KEY, 403, KEY, {}, None)
        client, opener, sleep = self.make_client([error])
        with self.assertRaises(feed.FeedError) as caught:
            client.get("test/v1", {})
        self.assertIn("403", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_invalid_and_oversized_responses(self):
        for body in (b"<html>maintenance</html>", b"[]", b"\xff", b"x" * (feed.MAX_RESPONSE_BYTES + 1)):
            client, _, _ = self.make_client([Response(body)])
            with self.subTest(length=len(body)), self.assertRaises(feed.FeedError):
                client.get("test/v1", {})

    def test_redirect_handler_does_not_forward_key(self):
        self.assertIsNone(feed.NoRedirects().redirect_request(None, None, 302, "", {},
                                                             "https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
