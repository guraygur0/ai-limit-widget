<div align="center">

# Ai Limit Widget

**Your Claude and Codex limits, quietly on your desktop.**

![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D6?logo=windows)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![UI](https://img.shields.io/badge/UI-PySide6-41CD52)

<img src="docs/preview.png" width="470" alt="Ai Limit Widget preview" />

*Illustrative quota values, not live account data.*

</div>

## Made for the desktop

Dark rounded cards, crisp vector rendering, a consistent blue accent and a quiet desktop presence. Each widget runs independently of ChatGPT or Codex, with no console window left open.

| Feature | What you get |
|---|---|
| At a glance | Session and weekly remaining quota, with reset countdowns. |
| Honest freshness | Successful timestamps, explicit stale data and provider backoff. |
| Independent providers | A slow request never blocks the other provider. |
| Local authentication | Reads existing CLI login; credentials are not shipped or stored in the repository. |

## Before you start

Windows desktop card built with PySide6. High-DPI vector rendering, draggable and lockable position, saved settings, and console-free logon startup.

Reads your existing Claude Code and Codex login locally. Sign in through those applications first. Expired login requires signing in again. Provider Retry-After is respected; old values are explicitly marked stale. This uses provider usage endpoints whose availability can change.

## Install

Windows 10/11 and Python 3.11+ (tested with Python 3.14). In PowerShell:

```powershell
git clone https://github.com/guraygur0/ai-limit-widget.git
cd ai-limit-widget
```

Then install and enable automatic startup:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

For an explicit interpreter: `-Python "C:\path\to\python.exe"`.
The installer installs requirements into that interpreter and registers `GurOS Usage Widget` for your Windows account. It waits 20 seconds at logon, allows battery operation and retries failed exits. Run only one copy.

## Use

Right-click for settings, size, position lock and Quit. Local settings/history/cache live in `state/` and are never included in Git. Startup failures are recorded in bounded `state/crash.log` files. Quitting normally stops the widget until the next launch/logon.

To remove autostart:
```powershell
Unregister-ScheduledTask -TaskName 'GurOS Usage Widget' -Confirm:$false
```

## Tests

```powershell
python -m unittest discover -s tests -q
```

Automated tests do not prove a real Windows reboot, a full focus session or all third-party playback/network behavior. No credentials, personal runtime state, screenshots or private history are distributed.

## Reliability and privacy

- Saved position and a right-click position lock.
- Desktop layer selection accounts for visible wallpaper WorkerW windows.
- A 20-second logon delay lets the Windows desktop initialize.
- Failed process exits retry through Task Scheduler; bounded local crash logs help diagnosis.
- 50 automated tests passed in this release. These are not a substitute for testing every Windows, account and wallpaper configuration.
- No analytics or telemetry service is added. Third-party requests are needed only for the widget's documented provider or playback features.

## The matching set

[AI limits](https://github.com/guraygur0/ai-limit-widget) · [Focus timer](https://github.com/guraygur0/focus-timer-widget) · [Mini player](https://github.com/guraygur0/mini-player-widget)

Found a problem? Open an issue with your Windows version, display scaling and steps to reproduce. Remove account identifiers and personal content before attaching logs.
