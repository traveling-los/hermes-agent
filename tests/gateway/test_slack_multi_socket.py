"""Tests for multi-workspace Socket Mode support in the Slack adapter."""
import pytest

from plugins.platforms.slack.adapter import parse_socket_pairs


def test_single_pair_legacy():
    assert parse_socket_pairs("xoxb-a", "xapp-1") == [("xoxb-a", "xapp-1")]


def test_two_pairs_positional():
    assert parse_socket_pairs("xoxb-a, xoxb-b", "xapp-1, xapp-2") == [
        ("xoxb-a", "xapp-1"),
        ("xoxb-b", "xapp-2"),
    ]


def test_extra_bot_tokens_stay_outbound_only():
    # 3 bot tokens, 1 app token → one socket on the first pair.
    assert parse_socket_pairs("xoxb-a,xoxb-b,xoxb-c", "xapp-1") == [
        ("xoxb-a", "xapp-1")
    ]


def test_more_app_than_bot_tokens_is_config_error():
    assert parse_socket_pairs("xoxb-a", "xapp-1,xapp-2") is None


def test_empty_app_tokens_is_config_error():
    assert parse_socket_pairs("xoxb-a", "") is None
    assert parse_socket_pairs("xoxb-a", " , ") is None


def test_whitespace_and_trailing_commas_tolerated():
    assert parse_socket_pairs(" xoxb-a ,", " xapp-1 ,") == [("xoxb-a", "xapp-1")]
