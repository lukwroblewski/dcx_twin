import asyncio
import logging
import random
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SIMULATOR] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DEVICE_ID = 1

# Mapa rejestrów Holding Registers (wartości x10 dla precyzji 0.1)
REG_FWS_SUPPLY_TEMP = 0    # Temp. wody pierwotnej [°C x10]
REG_TCS_SUPPLY_TEMP = 1    # Temp. zasilania serwerów [°C x10]
REG_TCS_RETURN_TEMP = 2    # Temp. powrotu z serwerów [°C x10]
REG_TCS_FLOW_LPM = 3       # Przepływ całkowity [L/min x10]
REG_FILTER_DP_KPA = 4      # Różnica ciśnień na filtrze [kPa x10]
REG_IT_LOAD_KW = 5         # Obciążenie cieplne serwerów [kW]
REG_PUMP1_SPEED = 6        # Prędkość pompy 1 [%]
REG_PUMP1_STATUS = 7       # 0=OFF/STBY, 1=RUNNING, 2=FAULT
REG_PUMP2_SPEED = 8        # Prędkość pompy 2 (rezerwowa N+1) [%]
REG_PUMP2_STATUS = 9       # 0=OFF/STBY, 1=RUNNING, 2=FAULT
REG_SETPOINT_TEMP = 10     # RW: Nastawa temp. zasilania TCS [°C x10]
REG_CMD_FAULT_PUMP1 = 11   # RW: 0=Normal, 1=Symuluj awarię pompy 1
REG_ALARM_CODE = 12        # 0=OK, 1=P1_FAILOVER, 2=HIGH_TEMP, 3=P2_FAULT, 4=BOTH_PUMPS_FAULT
REG_CMD_FAULT_PUMP2 = 13   # RW: 0=Normal, 1=Symuluj awarię pompy 2
REG_FWS_VALVE_PCT = 14     # Otwarcie zaworu pierwotnego FWS [%]


async def physics_loop(server: ModbusTcpServer) -> None:
    fws_supply = 19.5
    tcs_supply = 32.0
    tcs_return = 42.0
    flow_lpm = 182.0
    it_load_kw = 120.0
    setpoint = 32.0
    valve_integral = 5.0
    fws_valve_pct = 42.0

    # Inicjalizacja rejestrów sterowalnych
    await server.async_setValues(DEVICE_ID, 3, REG_SETPOINT_TEMP, [int(setpoint * 10)])
    await server.async_setValues(DEVICE_ID, 3, REG_CMD_FAULT_PUMP1, [0])
    await server.async_setValues(DEVICE_ID, 3, REG_CMD_FAULT_PUMP2, [0])

    while True:
        rw_regs = await server.async_getValues(DEVICE_ID, 3, REG_SETPOINT_TEMP, 4)
        setpoint = max(18.0, min(45.0, rw_regs[0] / 10.0))
        fault_p1 = rw_regs[1] == 1
        fault_p2 = rw_regs[3] == 1

        # Delikatne wahania obciążenia klastra AI/HPC i wody pierwotnej
        it_load_kw = max(90.0, min(180.0, it_load_kw + random.uniform(-1.5, 1.5)))
        fws_supply = max(17.5, min(22.0, fws_supply + random.uniform(-0.05, 0.05)))

        # Dobór obrotów pompy wtórnej pod obciążenie cieplne (utrzymanie dT ~ 10°C)
        desired_pump_speed = int(max(45, min(95, 35 + (it_load_kw / 4.0))))

        # Logika redundancji N+1 + obsługa awarii obu pomp
        if fault_p1 and fault_p2:
            pump1_status, pump1_speed = 2, 0
            pump2_status, pump2_speed = 2, 0
            active_speed = 0
            alarm_code = 4  # CRITICAL_BOTH_PUMPS_FAULT
        elif fault_p1:
            pump1_status, pump1_speed = 2, 0
            pump2_status, pump2_speed = 1, desired_pump_speed
            active_speed = pump2_speed
            alarm_code = 1  # PUMP1_FAILOVER_ACTIVE
        elif fault_p2:
            pump1_status, pump1_speed = 1, desired_pump_speed
            pump2_status, pump2_speed = 2, 0
            active_speed = pump1_speed
            alarm_code = 3  # PUMP2_FAULT_NO_REDUNDANCY
        else:
            pump1_status, pump1_speed = 1, desired_pump_speed
            pump2_status, pump2_speed = 0, 0
            active_speed = pump1_speed
            alarm_code = 0

        # Bezwładność hydrauliczna przepływu (rozbieg/wybieg wirnika pompy)
        target_flow = active_speed * 2.8
        flow_lpm += (target_flow - flow_lpm) * 0.45
        if flow_lpm < 1.0 and active_speed == 0:
            flow_lpm = 0.0
        elif active_speed > 0:
            flow_lpm = max(0.0, flow_lpm + random.uniform(-0.8, 0.8))

        # Regulator PI zaworu pierwotnego FWS (chłodzi obieg TCS przez wymiennik)
        temp_error = tcs_supply - setpoint
        valve_integral = max(-30.0, min(55.0, valve_integral + temp_error * 0.8))
        fws_valve_pct = max(5.0, min(100.0, 35.0 + temp_error * 10.0 + valve_integral))

        # Fizyka wymiany ciepła z bezwładnością termiczną
        if flow_lpm > 5.0:
            # Delta T po stronie serwerów: Q / (m * cp), gdzie dla wody/glikolu ~ 15 * kW / (L/min)
            delta_t_servers = (it_load_kw * 15.0) / flow_lpm
            target_return = tcs_supply + delta_t_servers
            tcs_return += (target_return - tcs_return) * 0.12

            # Sprawność wymiennika płytowego zależna od otwarcia zaworu FWS (max 87%)
            hx_effectiveness = 0.12 + 0.75 * (fws_valve_pct / 100.0)
            hx_outlet_eq = tcs_return - hx_effectiveness * max(0.0, tcs_return - fws_supply)
            tcs_supply += (hx_outlet_eq - tcs_supply) * 0.08 + random.uniform(-0.03, 0.03)
        else:
            # Brak przepływu (awaria obu pomp): woda stoi w blokach serwerów i szybko się nagrzewa
            tcs_return = min(95.0, tcs_return + (it_load_kw / 240.0))
            tcs_supply = min(90.0, tcs_supply + 0.12)

        if tcs_supply > setpoint + 3.0 and alarm_code == 0:
            alarm_code = 2  # HIGH_TCS_SUPPLY_TEMP

        filter_dp = (flow_lpm / 200.0) ** 1.5 * 16.0 if flow_lpm > 1.0 else 0.0

        registers = [
            int(round(fws_supply * 10)),
            int(round(tcs_supply * 10)),
            int(round(tcs_return * 10)),
            int(round(flow_lpm * 10)),
            int(round(filter_dp * 10)),
            int(round(it_load_kw)),
            pump1_speed,
            pump1_status,
            pump2_speed,
            pump2_status,
            int(round(setpoint * 10)),
            1 if fault_p1 else 0,
            alarm_code,
            1 if fault_p2 else 0,
            int(round(fws_valve_pct)),
        ]
        await server.async_setValues(DEVICE_ID, 3, 0, registers)

        log.info(
            "TCS=%.1f/%.1f°C (set=%.1f°C, valve=%d%%) | Flow=%.1f L/min | P1=%s(%d%%) P2=%s(%d%%) | Alarm=%d",
            tcs_supply, tcs_return, setpoint, int(round(fws_valve_pct)), flow_lpm,
            "FAULT" if pump1_status == 2 else "RUN", pump1_speed,
            "FAULT" if pump2_status == 2 else ("RUN" if pump2_status == 1 else "STBY"),
            pump2_speed,
            alarm_code,
        )
        await asyncio.sleep(1.0)


async def main() -> None:
    device = SimDevice(
        id=DEVICE_ID,
        simdata=SimData(address=0, count=16, values=0, datatype=DataType.REGISTERS),
    )
    server = ModbusTcpServer(context=device, address=("0.0.0.0", 5020))
    await server.serve_forever(background=True)
    log.info("CDU Modbus TCP Simulator nasłuchuje na porcie 5020 (device_id=%d)", DEVICE_ID)

    try:
        await physics_loop(server)
    finally:
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())