import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from pydantic import BaseModel, Field
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EDGE-GATEWAY] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

MODBUS_HOST = os.getenv("MODBUS_HOST", "localhost")
MODBUS_PORT = int(os.getenv("MODBUS_PORT", "5020"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SEC", "1.0"))
DEVICE_ID = 1

INFLUX_URL = os.getenv("INFLUX_URL", "")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "dcx")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "cdu_telemetry")

PUMP_STATUS_MAP = {0: "StandbyOffline", 1: "Enabled", 2: "Disabled"}
PUMP_HEALTH_MAP = {0: "OK", 1: "OK", 2: "Critical"}
ALARM_MAP = {
    0: "None",
    1: "PUMP1_FAILOVER_ACTIVE",
    2: "HIGH_TCS_SUPPLY_TEMP",
    3: "PUMP2_FAULT_NO_REDUNDANCY",
    4: "CRITICAL_BOTH_PUMPS_FAULT",
}


class GatewayState:
    def __init__(self) -> None:
        self.client: AsyncModbusTcpClient | None = None
        self.lock: asyncio.Lock | None = None
        self.connected = False
        self.last_error: str | None = None
        self.latest: dict[str, Any] | None = None
        self.influx_buffer: deque[Point] = deque(maxlen=600)
        self.influx_client: InfluxDBClient | None = None
        self.write_api = None

        if INFLUX_URL and INFLUX_TOKEN:
            self.influx_client = InfluxDBClient(
                url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=2_000
            )
            self.write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)

    def ensure_modbus(self) -> tuple[AsyncModbusTcpClient, asyncio.Lock]:
        if self.lock is None:
            self.lock = asyncio.Lock()
        if self.client is None:
            self.client = AsyncModbusTcpClient(
                host=MODBUS_HOST, port=MODBUS_PORT, timeout=2.0
            )
        return self.client, self.lock


state = GatewayState()


def decode_registers(regs: list[int]) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fws_supply_temp_c": regs[0] / 10.0,
        "tcs_supply_temp_c": regs[1] / 10.0,
        "tcs_return_temp_c": regs[2] / 10.0,
        "tcs_flow_lpm": regs[3] / 10.0,
        "filter_dp_kpa": regs[4] / 10.0,
        "it_load_kw": regs[5],
        "pumps": [
            {
                "id": "Pump1",
                "role": "Primary",
                "speed_pct": regs[6],
                "state": PUMP_STATUS_MAP.get(regs[7], "Unknown"),
                "health": PUMP_HEALTH_MAP.get(regs[7], "Warning"),
            },
            {
                "id": "Pump2",
                "role": "Redundant_N+1",
                "speed_pct": regs[8],
                "state": PUMP_STATUS_MAP.get(regs[9], "Unknown"),
                "health": PUMP_HEALTH_MAP.get(regs[9], "Warning"),
            },
        ],
        "setpoint_temp_c": regs[10] / 10.0,
        "pump1_fault_cmd": bool(regs[11]),
        "alarm_code": regs[12],
        "alarm_message": ALARM_MAP.get(regs[12], "Unknown"),
        "pump2_fault_cmd": bool(regs[13]),
        "fws_valve_pct": regs[14],
    }


def build_influx_point(data: dict[str, Any]) -> Point:
    return (
        Point("cdu_metrics")
        .tag("unit", "DCX-CDU-Sim-01")
        .field("fws_supply_temp_c", float(data["fws_supply_temp_c"]))
        .field("tcs_supply_temp_c", float(data["tcs_supply_temp_c"]))
        .field("tcs_return_temp_c", float(data["tcs_return_temp_c"]))
        .field("setpoint_temp_c", float(data["setpoint_temp_c"]))
        .field("tcs_flow_lpm", float(data["tcs_flow_lpm"]))
        .field("filter_dp_kpa", float(data["filter_dp_kpa"]))
        .field("it_load_kw", float(data["it_load_kw"]))
        .field("pump1_speed_pct", int(data["pumps"][0]["speed_pct"]))
        .field("pump2_speed_pct", int(data["pumps"][1]["speed_pct"]))
        .field("fws_valve_pct", int(data["fws_valve_pct"]))
        .field("alarm_code", int(data["alarm_code"]))
        .time(data["timestamp"])
    )


def flush_to_influx(point: Point) -> None:
    if not state.write_api:
        return
    state.influx_buffer.append(point)
    try:
        batch = list(state.influx_buffer)
        state.write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=batch)
        state.influx_buffer.clear()
    except Exception as exc:
        log.warning(
            "InfluxDB niedostępny (%s). Zbuforowano lokalnie: %d próbek",
            exc,
            len(state.influx_buffer),
        )


async def modbus_polling_loop() -> None:
    client, lock = state.ensure_modbus()
    while True:
        try:
            async with lock:
                if not client.connected:
                    await client.connect()

                res = await client.read_holding_registers(
                    address=0, count=15, device_id=DEVICE_ID
                )
                if res.isError():
                    raise ModbusException(f"Modbus error response: {res}")

                data = decode_registers(res.registers)
                state.latest = data
                state.connected = True
                state.last_error = None
            await asyncio.to_thread(flush_to_influx, build_influx_point(data))
        except Exception as exc:
            state.connected = False
            state.last_error = str(exc)
            log.warning("Błąd odczytu Modbus: %s", exc)
            client.close()

        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.ensure_modbus()
    task = asyncio.create_task(modbus_polling_loop())
    try:
        yield
    finally:
        task.cancel()
        if state.client:
            state.client.close()
        if state.influx_client:
            state.influx_client.close()


app = FastAPI(
    title="DCX CDU Edge Gateway (DMTF Redfish PoC)",
    description="Bramka brzegowa łącząca sterownik CDU (Modbus TCP) z warstwą DCIM (DMTF Redfish API) oraz InfluxDB.",
    version="0.3.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response

class SetpointRequest(BaseModel):
    target_temp_c: float = Field(..., ge=18.0, le=45.0, examples=[28.5])


class FaultSimulationRequest(BaseModel):
    pump1_fault: bool = Field(False, examples=[True])
    pump2_fault: bool = Field(False, examples=[False])


@app.get("/health")
def health():
    return {
        "status": "ok" if state.connected else "degraded",
        "modbus_connected": state.connected,
        "last_error": state.last_error,
        "influx_buffered_samples": len(state.influx_buffer),
    }


@app.get("/redfish/v1/")
def redfish_service_root():
    return {
        "@odata.id": "/redfish/v1/",
        "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
        "Id": "RootService",
        "Name": "DCX Liquid Cooling Redfish Service",
        "RedfishVersion": "1.18.0",
        "ThermalEquipment": {"@odata.id": "/redfish/v1/ThermalEquipment"},
    }


@app.get("/redfish/v1/ThermalEquipment")
def thermal_equipment():
    return {
        "@odata.id": "/redfish/v1/ThermalEquipment",
        "@odata.type": "#ThermalEquipment.v1_0_0.ThermalEquipment",
        "Name": "Thermal Equipment",
        "CDUs": {"@odata.id": "/redfish/v1/ThermalEquipment/CDUs"},
    }


@app.get("/redfish/v1/ThermalEquipment/CDUs")
def list_cdus():
    return {
        "@odata.id": "/redfish/v1/ThermalEquipment/CDUs",
        "@odata.type": "#CoolingUnitCollection.CoolingUnitCollection",
        "Name": "Cooling Distribution Units",
        "Members@odata.count": 1,
        "Members": [{"@odata.id": "/redfish/v1/ThermalEquipment/CDUs/1"}],
    }


@app.get("/redfish/v1/ThermalEquipment/CDUs/1")
def get_cdu_details():
    if not state.latest:
        raise HTTPException(status_code=503, detail="Brak danych z Modbus TCP")

    d = state.latest
    if d["alarm_code"] == 4:
        overall_health = "Critical"
    elif d["alarm_code"] > 0:
        overall_health = "Warning"
    else:
        overall_health = "OK"

    return {
        "@odata.id": "/redfish/v1/ThermalEquipment/CDUs/1",
        "@odata.type": "#CoolingUnit.v1_1_0.CoolingUnit",
        "Id": "1",
        "Name": "DCX-CDU-Sim-01",
        "EquipmentType": "CDU",
        "Model": "ECDU-600-DigitalTwin",
        "Status": {
            "State": "Enabled" if state.connected else "UnavailableOffline",
            "Health": overall_health,
        },
        "CoolingCapacityWatts": d["it_load_kw"] * 1000,
        "PrimaryCoolantConnectors": {
            "SupplyTemperatureCelsius": d["fws_supply_temp_c"],
            "ValvePositionPercent": d["fws_valve_pct"],
        },
        "SecondaryCoolantConnectors": {
            "SupplyTemperatureCelsius": d["tcs_supply_temp_c"],
            "ReturnTemperatureCelsius": d["tcs_return_temp_c"],
            "FlowLitersPerMinute": d["tcs_flow_lpm"],
            "FilterDifferentialPressureKPa": d["filter_dp_kpa"],
            "TargetSupplyTemperatureCelsius": d["setpoint_temp_c"],
        },
        "Pumps": d["pumps"],
        "Oem": {
            "DCX": {
                "AlarmCode": d["alarm_code"],
                "AlarmMessage": d["alarm_message"],
                "LastSampleTimestamp": d["timestamp"],
            }
        },
        "Actions": {
            "#CoolingUnit.SetTargetTemperature": {
                "target": "/redfish/v1/ThermalEquipment/CDUs/1/Actions/SetTargetTemperature"
            },
            "Oem": {
                "#DCX.SimulatePumpFault": {
                    "target": "/redfish/v1/ThermalEquipment/CDUs/1/Actions/SimulatePumpFault"
                }
            },
        },
    }


@app.post("/redfish/v1/ThermalEquipment/CDUs/1/Actions/SetTargetTemperature")
async def set_target_temperature(payload: SetpointRequest):
    reg_val = int(round(payload.target_temp_c * 10))
    client, lock = state.ensure_modbus()
    async with lock:
        if not client.connected:
            await client.connect()
        res = await client.write_register(address=10, value=reg_val, device_id=DEVICE_ID)
        if res.isError():
            raise HTTPException(status_code=502, detail="Błąd zapisu rejestru Modbus")
    return {"status": "accepted", "new_target_temp_c": payload.target_temp_c}


@app.post("/redfish/v1/ThermalEquipment/CDUs/1/Actions/SimulatePumpFault")
async def simulate_pump_fault(payload: FaultSimulationRequest):
    client, lock = state.ensure_modbus()
    async with lock:
        if not client.connected:
            await client.connect()
        res1 = await client.write_register(
            address=11, value=1 if payload.pump1_fault else 0, device_id=DEVICE_ID
        )
        res2 = await client.write_register(
            address=13, value=1 if payload.pump2_fault else 0, device_id=DEVICE_ID
        )
        if res1.isError() or res2.isError():
            raise HTTPException(status_code=502, detail="Błąd zapisu rejestrów Modbus")
    return {
        "status": "accepted",
        "pump1_fault_simulated": payload.pump1_fault,
        "pump2_fault_simulated": payload.pump2_fault,
    }