import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from fastapi.testclient import TestClient

import main
from main import app, decode_registers, state


def test_decode_registers_normal_failover_and_total_fault():
    regs_normal = [195, 320, 420, 1820, 145, 120, 65, 1, 0, 0, 320, 0, 0, 0, 42]
    decoded = decode_registers(regs_normal)
    assert decoded["tcs_supply_temp_c"] == 32.0
    assert decoded["fws_valve_pct"] == 42
    assert decoded["pumps"][0]["state"] == "Enabled"
    assert decoded["pumps"][1]["state"] == "StandbyOffline"
    assert decoded["alarm_message"] == "None"

    regs_failover = [195, 320, 420, 1820, 145, 120, 0, 2, 65, 1, 320, 1, 1, 0, 42]
    decoded_fail = decode_registers(regs_failover)
    assert decoded_fail["pumps"][0]["health"] == "Critical"
    assert decoded_fail["pumps"][1]["state"] == "Enabled"
    assert decoded_fail["alarm_message"] == "PUMP1_FAILOVER_ACTIVE"

    regs_both = [195, 340, 510, 0, 0, 120, 0, 2, 0, 2, 320, 1, 4, 1, 100]
    decoded_both = decode_registers(regs_both)
    assert decoded_both["tcs_flow_lpm"] == 0.0
    assert decoded_both["alarm_message"] == "CRITICAL_BOTH_PUMPS_FAULT"


def test_redfish_requires_valid_api_key_and_sets_security_headers(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(main, "REDFISH_API_KEY", "test-secret-key")
    state.connected = True
    state.latest = decode_registers([195, 340, 510, 0, 0, 120, 0, 2, 0, 2, 320, 1, 4, 1, 100])

    # Brak klucza -> 401
    unauth = client.get("/redfish/v1/ThermalEquipment/CDUs/1")
    assert unauth.status_code == 401
    assert unauth.headers["X-Content-Type-Options"] == "nosniff"
    assert unauth.headers["Cross-Origin-Resource-Policy"] == "same-origin"

    # Zły klucz -> 401
    wrong = client.get(
        "/redfish/v1/ThermalEquipment/CDUs/1", headers={"X-API-Key": "wrong"}
    )
    assert wrong.status_code == 401

    # Poprawny klucz -> 200
    ok = client.get(
        "/redfish/v1/ThermalEquipment/CDUs/1",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["Status"]["Health"] == "Critical"
    assert body["PrimaryCoolantConnectors"]["ValvePositionPercent"] == 100


def test_redfish_actions_write_modbus(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(main, "REDFISH_API_KEY", "test-secret-key")
    mock_write = AsyncMock(return_value=SimpleNamespace(isError=lambda: False))
    fake_modbus = SimpleNamespace(connected=True, write_register=mock_write)
    headers = {"X-API-Key": "test-secret-key"}

    with patch.object(
        state, "ensure_modbus", side_effect=lambda: (fake_modbus, asyncio.Lock())
    ):
        resp_temp = client.post(
            "/redfish/v1/ThermalEquipment/CDUs/1/Actions/SetTargetTemperature",
            json={"target_temp_c": 26.0},
            headers=headers,
        )
        assert resp_temp.status_code == 200
        mock_write.assert_awaited_with(address=10, value=260, device_id=1)

        resp_fault = client.post(
            "/redfish/v1/ThermalEquipment/CDUs/1/Actions/SimulatePumpFault",
            json={"pump1_fault": True, "pump2_fault": True},
            headers=headers,
        )
        assert resp_fault.status_code == 200
        mock_write.assert_has_awaits(
            [
                call(address=11, value=1, device_id=1),
                call(address=13, value=1, device_id=1),
            ]
        )