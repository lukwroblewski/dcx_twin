# DCX CDU Digital Twin – Edge Gateway & DMTF Redfish PoC

Skonteneryzowany prototyp oprogramowania brzegowego (IPC/Edge) dla jednostki dystrybucji chłodziwa (**CDU** – *Coolant Distribution Unit*) z obsługą protokołu **Modbus TCP**, zapisem szeregów czasowych do **InfluxDB**, wizualizacją w **Grafanie** oraz integracją Data Center poprzez **DMTF Redfish REST API**.

## Architektura

```mermaid
flowchart LR
    SIM["CDU Simulator<br/>(Python + pymodbus)<br/>Modbus TCP :5020"] <-->|"Holding Registers<br/>(N+1 Pumps, Temps, Alarms)"| GW["Edge Gateway<br/>(FastAPI + AsyncModbus)<br/>:8000"]
    GW -->|"Line Protocol + Local Buffer"| DB[("InfluxDB v2<br/>:8086")]
    DB -->|"Flux Queries"| GRAF["Grafana Dashboard<br/>:3000"]
    DCIM["DCIM / Swagger UI"] <-->|"DMTF Redfish JSON<br/>/redfish/v1/..."| GW