# Browser login workaround

Twitch currently rejects device-code requests made with this project's Android
client ID with HTTP 400 and `invalid client`. Reading `device_code` from that
response raises a `KeyError` before login completes. This workaround handles the
error explicitly and uses a dedicated Chrome/Edge profile for new logins.

## Signing in

1. Install Chrome or Edge. Windows is the platform tested with a real account.
2. Start the miner normally, without **Run as administrator**, and click **Login**.
3. Sign in directly on Twitch and complete any verification yourself. This first
   browser instance has no debugging connection or debugging command-line flags.
4. Close all windows of that browser, keeping the miner open. The miner reopens
   its dedicated profile minimized and uses Twitch's own browser requests.
5. Keep this second browser instance open or minimized. It closes when the miner
   shuts down. Add games to the priority list and reload to start mining.

Previously saved WEB sessions are restored without repeating the manual login.
Valid legacy Android sessions keep the existing request path. Unknown client
sessions and saved sessions affected by service failures are preserved.

The profile is stored in `browser_session/` next to the miner. It is separate
from the user's personal browser profiles. Both that directory and `cookies.jar`
contain private session data and must not be shared or committed. This change
does not read passwords or verification codes. The debugging port is random and
bound to loopback.

## Behavior and limitations

- Elevated Windows launches have a known startup failure. Chrome can relaunch
  itself without elevated privileges and exit its initial launcher process.
  The miner currently treats that process exit as the end of manual login and
  attempts to restart the browser while the login window is still running.
  This can produce "The browser did not start". Close the miner and its own
  browser windows, then start the miner normally. Handling the relaunched
  browser's lifetime remains unresolved; this workaround has not been
  separately verified in the affected Downloads installation.
- Browser GQL uses the headers supplied by Twitch in its own tab.
- The page reloads when captured headers need refreshing.
- A disconnected browser can restore the saved session. Interrupted allowlisted
  read-only operations retry once after reconnecting. The browser transport does
  not replay an interrupted mutation whose result is unknown.
- Proxy username/password authentication is not supported by this browser path.
- macOS and Linux are not verified with a real account. Browser discovery does
  not yet handle macOS application bundles.
- Headless/hidden operation is not part of this change. Separate test instances
  restored login and read inventory but received `failed integrity check` for
  the campaign dashboard. This does not establish which browser/profile factor
  caused the rejection. Integrity acceptance is not guaranteed across sessions.
- New user-facing error messages are currently in English; localization and
  broader compatibility need maintainer review.

## Validation

17 regression tests cover new login, WEB/Android restoration, expired sessions,
preservation of unsupported sessions and service failures, rejected device-code
responses, manual login without debugging flags, scoped header capture,
disconnected browser handling, bounded read retry, and no transport replay of
an interrupted mutation.

On Windows with Chrome, a real account logged in and connected to the WebSocket.
The miner then watched a channel and reported collecting the first two rewards
of a three-reward campaign, with progress displayed for the third. No account
identifiers or session files are included in this contribution. Long-duration
stability and other platforms remain unverified.

Install project and test dependencies, then run:

```console
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
python -m pytest -q
```

Related report: https://github.com/DevilXD/TwitchDropsMiner/issues/1165
