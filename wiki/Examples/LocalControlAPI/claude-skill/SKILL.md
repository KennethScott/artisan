---
name: artisan-roaster
description: Monitor, coach, or (when explicitly enabled) control a live coffee roast through Artisan's Local Control API on 127.0.0.1. Use when the user asks to watch, coach, or drive a roast in Artisan, check roast state, or mark roast events.
---

# Artisan roaster (template)

Template for a Claude Code skill. Copy this folder to `~/.claude/skills/artisan-roaster/` and fill in the
**Machine** section for your roaster. API reference: wiki/LocalControlAPI.md in the Artisan repository.

The API is the only way to touch the roaster. Never talk to the machine directly and never edit Artisan's
settings during a roast.

## Connection

- Base URL `http://127.0.0.1:8081/api/v1`. If a token is configured, send `Authorization: Bearer <token>` with the
  token from the `ARTISAN_API_TOKEN` environment variable; never print it.
- Reads: `GET /state`, `GET /wait?timeout=30&until=event|sample`.
- Writes: `/pid`, `/background`, `/sv`, `/slider`, `/event`, `/stop`, `/release` (see the reference).

## Machine (fill in)

| Slider | Name | Controls | Notes |
|---|---|---|---|
| 0 | | | |
| 1 | | | |
| 2 | | | |
| 3 | | | |

- PID type (`pid.kind`), what it controls (BT/ET), its resolution:
- Manual steps the roaster does that the API cannot (e.g. switching heating on):
- Alarms that switch the PID or move sliders during a roast:

At the start, confirm the slider names against `/state`; if they don't match, stop and ask.

## Modes

**Advisory (default).** Only read. Suggest concrete moves ("shift the background +5 °F now") with a one-clause reason.

**Control.** Only when the user says so explicitly in this session, and only within the scope they give
(e.g. "background shifts only"). Control ends when they say so, when `control.override`, `control.watchdog_tripped`
or `control.stopped` becomes true, or at DROP.

## Control levels: prefer the highest available

1. **Follow the background** (`/pid {"mode": "background"}`): Artisan's PID tracks the background's curve. Coach by
   comparing BT and RoR with `pid.sv`, and nudge with `/background` (`shift_temp` ±3–5 °; `shift_time` + = later
   reference, which while BT rises means a cooler target). One nudge per decision, then wait about 60 s.
2. **Setpoint** (`/pid {"mode": "manual"}` + `/sv`): move the target yourself, at most every 30 s.
3. **Actuators** (`/slider`): fan, drum; the heat slider only while the PID is off.

## Loop

1. `GET /state` once: unit, recording, background loaded and its title, `pid.available` and `pid.mode`,
   `control.fallback` (null means `/stop` cannot cut heat: tell the user).
2. Repeat `GET /wait?timeout=30&until=event`: one short line per tick (time, BT, RoR, gap to target, SV and PID
   state, new events), then decide: no change / suggest / act.

## Rules

- A `409` means locked or not possible: don't retry, report, and drop to advisory.
- If `control.override` becomes true, the user (or an alarm, or the machine) took over: stop writing and say what
  changed. Never call `/release` without the user's OK.
- A `422` on `/background` means the move would make the target jump: use smaller steps.
- Never mark CHARGE or DROP yourself; suggest them.
- If readings look wrong (null, frozen, implausible jumps), stop acting and tell the user.
- Emergency (runaway temperature, wrong readings, user says stop): `POST /stop`, then tell the user what happened.
