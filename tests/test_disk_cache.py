"""Tests for the on-disk EasyEDA component cache."""

import os
import time
from unittest import mock

import pytest

from kicad_jlcimport.easyeda import api, disk_cache


def test_round_trip():
    disk_cache.put("components", "abc123", {"title": "LED0603", "n": [1, 2]})
    assert disk_cache.get("components", "abc123") == {"title": "LED0603", "n": [1, 2]}


def test_missing_entry():
    assert disk_cache.get("components", "nope") is None


def test_expired_entry_is_ignored():
    disk_cache.put("uuids", "C1", [{"component_uuid": "x"}])
    path = disk_cache._path("uuids", "C1")
    old = time.time() - disk_cache.MAX_AGE - 60
    os.utime(path, (old, old))
    assert disk_cache.get("uuids", "C1") is None


def test_unsafe_keys_are_hashed():
    disk_cache.put("components", "../escape", 1)
    path = disk_cache._path("components", "../escape")
    assert os.path.dirname(path) == os.path.join(disk_cache.cache_dir(), "components")
    assert disk_cache.get("components", "../escape") == 1


def test_corrupt_entry_reads_as_missing():
    disk_cache.put("components", "bad", 1)
    with open(disk_cache._path("components", "bad"), "wb") as f:
        f.write(b"not gzip")
    assert disk_cache.get("components", "bad") is None


def test_unwritable_cache_is_ignored(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(disk_cache.CACHE_DIR_ENV, str(blocker / "cache"))
    disk_cache.put("components", "k", 1)  # no exception
    assert disk_cache.get("components", "k") is None


def test_unserialisable_value_is_ignored():
    disk_cache.put("components", "k", object())
    assert disk_cache.get("components", "k") is None
    assert not [n for n in os.listdir(os.path.join(disk_cache.cache_dir(), "components")) if n.endswith(".tmp")]


def test_clear():
    disk_cache.put("components", "k", 1)
    disk_cache.clear()
    assert disk_cache.get("components", "k") is None


@pytest.mark.parametrize("platform, env, expected", [("linux", {"XDG_CACHE_HOME": "/xdg"}, "/xdg/kicad_jlcimport")])
def test_platform_cache_dir(monkeypatch, platform, env, expected):
    monkeypatch.delenv(disk_cache.CACHE_DIR_ENV, raising=False)
    monkeypatch.setattr(disk_cache.sys, "platform", platform)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert disk_cache.cache_dir() == expected


class TestApiUsesCache:
    def test_component_data_fetched_once(self):
        with mock.patch.object(api, "_get_json", return_value={"result": {"title": "X"}}) as get:
            assert api.fetch_component_data("u1") == {"title": "X"}
            assert api.fetch_component_data("u1") == {"title": "X"}
        assert get.call_count == 1

    def test_component_uuids_fetched_once(self):
        result = [{"component_uuid": "s"}, {"component_uuid": "f"}]
        with mock.patch.object(api, "_get_json", return_value={"success": True, "result": result}) as get:
            assert api.fetch_component_uuids("C2286") == result
            assert api.fetch_component_uuids("C2286") == result
        assert get.call_count == 1

    def test_failures_are_not_cached(self):
        with mock.patch.object(api, "_get_json", return_value={"success": False}):
            with pytest.raises(api.APIError):
                api.fetch_component_uuids("C9")
        result = [{"component_uuid": "f"}]
        with mock.patch.object(api, "_get_json", return_value={"success": True, "result": result}) as get:
            assert api.fetch_component_uuids("C9") == result
        assert get.call_count == 1


def test_damaged_gzip_stream_reads_as_missing_and_is_removed():
    disk_cache.put("components", "torn", {"a": "x" * 2000})
    path = disk_cache._path("components", "torn")
    data = bytearray(open(path, "rb").read())
    data[20:40] = b"\x00" * 20  # valid gzip header, damaged deflate stream (zlib.error)
    with open(path, "wb") as f:
        f.write(bytes(data))
    assert disk_cache.get("components", "torn") is None
    assert not os.path.exists(path)
