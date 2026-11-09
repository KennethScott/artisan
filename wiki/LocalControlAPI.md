# Local Control API

____
**Status: experimental.** The API is developed on the `local-control-api` branch of a fork and has been tested with the
Artisan simulator and on a Kaleido M2. Endpoints and fields may still change.
____

### Introduction

The Local Control API is a small HTTP/JSON interface built into Artisan. It lets a program running on the same
computer **read the live roast** and, if you allow it, **act on it the way you would in the UI**: move the event
sliders, switch the PID on or off, change its target, shift the background profile, and mark roast events.

Artisan stays in charge of the machine. Every write goes through the same code paths as a click in the UI, so
the configured slider and button actions fire, and it works with any machine Artisan can drive (Kaleido, MODBUS,
S7, Hottop, TC4, …). The API never talks to the roaster directly.

Typical uses:

| Use | What a client does | Endpoints |
|---|---|---|
| **Monitoring** | Live dashboards, logging, notifications ("BT 380 °F, first crack due"), phone alerts | `GET /state`, `GET /wait` |
| **Advising** | A person or an AI assistant watches the roast and suggests moves; the roaster makes them | read endpoints only |
| **Following a profile** | The PID follows the background profile; the client nudges it ("run 5 °F hotter from here", "stretch the finish") | `/pid`, `/background` |
| **Setpoint control** | The client moves the PID target (SV) itself, e.g. every 30 s | `/pid`, `/sv` |
| **Actuator control** | The client moves heat, fan and drum sliders directly | `/slider` |
| **Event marking** | Mark CHARGE, DRY, FCs, … from an external detector (e.g. first-crack audio detection) | `/event` |

The higher levels in this table are generally the better choice for a remote client: a PID closes the fast loop
in Artisan or on the machine, the client only moves targets, and if the client goes silent the PID still holds a
sensible temperature.


### Enabling the API

1. Switch Artisan to Expert mode if needed (**Config » Mode » Expert**); the Curves dialog is not shown otherwise.
2. Open **Config » Curves… » UI**. In the **Local Control API** group set the port (default 8081) and optionally a
   token, then tick the checkbox. The URL of the state endpoint is shown once the API runs.
3. Optionally open **Settings…** (only while the API is off) to configure the write limits and safety fallback
   described below.

The API only listens on `127.0.0.1`: it is reachable from programs on the same computer, not from the network.
If a token is set, every request must carry `Authorization: Bearer <token>`.

Check that it runs:

```
curl -s http://127.0.0.1:8081/api/v1/state
```


### Safety model

Reading is always allowed. Writing is guarded by these rules, all enforced inside Artisan:

* **Limits.** Slider values are snapped to the slider's step size and clamped to its min/max; the SV is clamped to
  the PID's SV slider limits. Optional maximum step sizes per write (slider, SV) and a minimum interval between
  writes to the same target.
* **Manual override lock.** After the API has set something (a slider, the SV, the PID state or mode, the
  background position), a change by anything else locks all writes: the user moving it in the UI, an alarm, a
  quantifier following a value the machine reports, a ramp/soak program. Writes return `409` until
  `POST /release`. Small differences caused by rounding are ignored (one quantifier step for sliders, 0.5° for
  the SV; 3 °F / 1.5 °C for the Kaleido, which keeps its target in whole °C).
* **The PID owns its heat slider.** While the PID is on, the slider it drives (the PID's positive target) is not
  checked for overrides and the API refuses to write it.
* **No target jumps.** A background move is refused if it would change the PID target by more than the max SV
  step (20 °F / 11 °C if unset). Moving a background in time can land the PID's lookup point on a very different
  part of the curve, e.g. before the background's CHARGE at charge temperature.
* **Switching off is always allowed.** `POST /pid {"on": false}` and `POST /stop` are accepted even while locked.
* **Emergency stop.** `POST /stop` switches the PID off, applies the *fallback* (by default the PID's heat slider
  to its minimum) and locks all writes until `/release`.
* **Watchdog** (optional). If the API has written something during a recording and then no request arrives for
  the configured time, the fallback is applied (optionally also PID off) and writes are locked. A pending
  `/wait` long-poll counts as alive.

Machine-specific switches that are not sliders or PID functions (e.g. a heating on/off button) are deliberately
not reachable through the API; they stay manual.

#### Settings

**Config » Curves… » UI » Local Control API » Settings…**

| Setting | Default | Meaning |
|---|---|---|
| Min interval | 1.0 s | Minimum time between two writes to the same slider, the SV, the PID or the background |
| Max slider step | unlimited | Largest slider change accepted in one write |
| Max SV step | unlimited | Largest SV change per write; also the target-jump limit for background moves (20 °F / 11 °C if unlimited) |
| Fallback | Auto | Applied by STOP and the watchdog. *Auto*: the PID's heat slider set to its minimum. *None*, or an explicit slider and value. |
| Watchdog | off | Seconds of silence after which the fallback is applied |
| Switch PID off | off | The watchdog also switches the PID off (STOP always does) |

If no fallback can be resolved (no PID heat slider configured and none chosen), Artisan shows a message when the
API starts, and `/stop` only switches the PID off.


### Endpoints

Base URL `http://127.0.0.1:<port>/api/v1`. All bodies are JSON. Errors return `{"error": "<message>"}`.

| Method | Path | Body / query | Purpose |
|---|---|---|---|
| GET | `/state` | | Current snapshot (below) |
| GET | `/wait` | `timeout` (s, ≤ 120, default 30), `until` = `event` (default) or `sample` | Long-poll: returns `{"reason": "event"\|"sample"\|"timeout"\|"shutdown", "state": …}` on the next roast event, the next sample, the timeout, or when the API is stopped |
| POST | `/slider` | `{"slider": 0-3, "value": n}` | Move an event slider (fires its action) |
| POST | `/sv` | `{"value": n}` | Set the PID target. Only in manual SV mode. |
| POST | `/pid` | `{"on": bool, "mode": "manual"\|"background", "lookahead": 0-60}` | Any subset; applied as lookahead, mode, then on |
| POST | `/background` | `{"shift_temp": n, "shift_time": s}` | Move the background profile up (+) / down, later (+) / earlier |
| POST | `/event` | `{"event": "CHARGE"\|"DRY"\|"FCs"\|"FCe"\|"SCs"\|"SCe"\|"DROP"\|"COOL"}` | Mark an event (while recording; fires the button's action) |
| POST | `/stop` | | Emergency stop (see safety model) |
| POST | `/release` | | Clear the override, watchdog or stop lock |

Status codes: `400` invalid input · `401` missing/wrong token · `409` locked, not recording, no PID configured,
SV not in manual mode, no background loaded, or slider driven by the PID · `422` step or target jump too large ·
`429` written too soon.

#### State

```json
{
  "api_version": 1,
  "unit": "F",
  "monitoring": true,
  "recording": true,
  "charged": true,
  "time": 312.0,
  "bt": 330.3, "et": 333.1, "bt_ror": 23.7, "et_ror": 18.2,
  "bt_background": 335.4,
  "events": {"CHARGE": {"time": 0.0, "bt": 337.5}, "DRY": null, "FCs": null, "...": null},
  "sliders": [
    {"slider": 0, "name": "Air", "value": 20, "min": 0, "max": 100, "step": 5, "visible": true},
    {"slider": 3, "name": "Burner", "value": 35, "min": 0, "max": 100, "step": 5, "visible": true}
  ],
  "pid": {"available": true, "active": true, "sv": 332.9, "sv_min": 0.0, "sv_max": 446.0,
          "mode": "background", "source": "BT", "kind": "kaleido", "sv_tolerance": 3.0, "lookahead": 8},
  "background": {"loaded": true, "title": "Colombia washed v1", "charged": true,
                 "shift_temp": 5, "shift_time": 7, "unit": "F"},
  "control": {"override": false, "override_reason": "", "watchdog_armed": false, "watchdog_tripped": false,
              "watchdog_timeout": 0.0, "stopped": false, "fallback": {"slider": 3, "value": 0}}
}
```

* `time`: seconds since CHARGE (since START before CHARGE; `null` while only monitoring).
* `bt_ror`, `et_ror`: rate of rise per minute.
* `bt_background`: the background's BT at the same time after CHARGE (ignores time shifts). With the PID
  following the background, `pid.sv` is the actual target.
* `pid.kind`: `software` (Artisan's PID driving a slider), `modbus`, `s7`, `tc4`, `kaleido`. Fuji PIDs are reported
  as unavailable for now.
* `control.fallback`: what `/stop` and the watchdog will set, or `null`.


### Examples

Follow the roast, one line per event or 30 s:

```
curl -s "http://127.0.0.1:8081/api/v1/wait?timeout=30&until=event"
```

Profile following: the PID follows the background, the client nudges it.

```
curl -s -X POST http://127.0.0.1:8081/api/v1/pid -H "Content-Type: application/json" -d '{"mode": "background", "on": true}'
curl -s -X POST http://127.0.0.1:8081/api/v1/background -H "Content-Type: application/json" -d '{"shift_temp": 5}'
```

Emergency stop:

```
curl -s -X POST http://127.0.0.1:8081/api/v1/stop
```

More in [Examples/LocalControlAPI](Examples/LocalControlAPI):

* `monitor.py`: a dependency-free Python monitor that prints one line per sample interval and per event.
* `claude-skill/SKILL.md`: a template for using the API with Claude Code as a roast monitor, coach or controller.


### Notes from testing on a Kaleido M2

* The Kaleido's own PID is used when *Kaleido PID* is enabled; PID ON in Artisan switches the machine to its auto
  heating mode, and the machine holds BT on the SV closely. Artisan copies the machine's target back into the SV
  every sample, so the SV is quantized to whole °C.
* Heater power commands only took effect while heating was switched on at the machine; the PID's auto mode
  appeared to heat without it. Switching heating on set a drum speed below 40 to 40.
* With event quantifiers on, the sliders follow what the machine reports; changes the machine makes on its own
  then appear as manual overrides, which is intended.
* Alarms that switch the PID or move sliders (e.g. PID OFF at CHARGE, PID ON after the turning point) act like a
  user and engage the override lock for whatever the API had set. Let alarms and the API own different things.


### Design

The API runs an aiohttp server on its own asyncio thread (`src/artisanlib/webapi.py`). Reads take a consistent
snapshot of Artisan's lists; writes are queued Qt signals (`addEventSignal`, `setSVSignal`, `pidOnSignal`,
`moveBackgroundSignal`, …) and therefore execute in the GUI thread exactly like UI actions. `/wait` is woken by a
hook called after each sample. Settings are stored in the `WebAPI` group of Artisan's settings.
