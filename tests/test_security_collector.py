"""Tests for the Cyber Security & Canary Watchdog collector and actions."""
from unittest.mock import MagicMock, patch

import pytest
from sol_control_hud.data.collectors import security


def test_get_security_status_parsing():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "summary": {
            "status": "secure",
            "canary_active": True,
            "canary_ports": [2323, 4444],
            "gateway_status": "verified",
            "known_device_count": 6,
            "unknown_device_count": 0,
            "ipad_sound_alert": True,
        },
        "settings": {
            "canary_enabled": True,
            "arp_watchdog_enabled": True,
            "expected_gateway_ip": "192.168.0.1",
            "ipad_sound_alert": True,
        },
        "alerts": [],
        "devices": [],
    }

    with patch.object(security, "_client") as mock_client:
        mock_client.return_value.get.return_value = mock_resp
        res = security.get_security_status()
        assert res["up"] is True
        assert res["summary"]["status"] == "secure"
        assert res["summary"]["canary_active"] is True
        assert res["settings"]["canary_enabled"] is True


def test_get_security_status_offline_fallback():
    with patch.object(security, "_client") as mock_client:
        mock_client.return_value.get.side_effect = Exception("Connection refused")
        res = security.get_security_status()
        assert res["up"] is False
        assert res["status"] == "offline"
        assert res["summary"]["canary_active"] is False


def test_toggle_security_feature():
    status_mock = {
        "up": True,
        "summary": {"canary_active": True},
        "settings": {
            "canary_enabled": True,
            "arp_watchdog_enabled": True,
            "unknown_device_alerts": False,
            "ipad_sound_alert": True,
        },
    }

    with patch.object(security, "get_security_status", return_value=status_mock), \
         patch.object(security, "_client") as mock_client:
        post_mock = MagicMock()
        post_mock.status_code = 200
        mock_client.return_value.post.return_value = post_mock

        # Test toggling canary
        res = security.toggle_security_feature("canary")
        assert res["ok"] is True
        args, kwargs = mock_client.return_value.post.call_args
        assert kwargs["json"]["action"] == "update_settings"
        assert kwargs["json"]["settings"]["canary_enabled"] is False

        # Test toggling all (should turn off because canary_active is True)
        res = security.toggle_security_feature("all")
        assert res["ok"] is True
        args, kwargs = mock_client.return_value.post.call_args
        assert kwargs["json"]["action"] == "update_settings"
        assert kwargs["json"]["settings"]["canary_enabled"] is False
        assert kwargs["json"]["settings"]["arp_watchdog_enabled"] is False


def test_trigger_alarm_and_ack():
    with patch.object(security, "_client") as mock_client:
        post_mock = MagicMock()
        post_mock.status_code = 200
        mock_client.return_value.post.return_value = post_mock

        res_alarm = security.trigger_test_alarm()
        assert res_alarm["ok"] is True

        res_ack = security.acknowledge_alerts()
        assert res_ack["ok"] is True
