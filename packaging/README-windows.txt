Meniscus for Windows (portable)
===============================

Run Meniscus.exe. It opens in a borderless Chrome, Edge or Brave window. Edge is part of
Windows, so there is nothing else to install. The refracting glass edges only render in these
Chromium-based browsers.

Everything Meniscus keeps lives in the "data" folder next to Meniscus.exe:

  data\gallery-dl\config.json   gallery-dl config (Settings has a button to open it)
  data\library\                 library index and download history
  data\cache\thumbs\            thumbnails
  data\config\settings.json     Meniscus settings
  data\browser\                 the app window's browser profile
  data\cookies.txt              site logins from the sign-in window
  data\login-browser\           the sign-in window's browser profile
  data\meniscus.log             log, for when something goes wrong

Downloads go to Downloads\gdl and Discord copies to Downloads\discord-ready, unless you change
them (extractor.base-directory in the gallery-dl config, and the export folder in Settings).
To move Meniscus, move the whole folder. If the folder is somewhere read-only, such as
Program Files, the data goes to %LOCALAPPDATA%\Meniscus instead.

When you close the window, Meniscus finishes any downloads that are still running and then
exits by itself. Starting Meniscus.exe again while it is still running just opens a new window.

ffmpeg and ffprobe are needed for video thumbnails and Discord copies. Meniscus uses the ones
next to Meniscus.exe if they are there, otherwise the ones on your PATH. One way to install them:
  winget install Gyan.FFmpeg

Logging in to sites (Instagram and others): open Settings, go to Site logins, and press
Sign in. A failed download that needs a login also shows a Sign in button. Log in to the site in
the window that opens, close the window, then press Retry on the download. Meniscus saves the
login to data\cookies.txt and data\login-browser, so keep the data folder private. Forget
deletes both. Meniscus can't read your normal Chrome or Edge logins, because those browsers
encrypt their cookies in a way gallery-dl and yt-dlp can't read on Windows.

meniscus-engine.exe is the bundled gallery-dl and yt-dlp, which Meniscus runs for downloads.
You can also use it yourself from a terminal:
  meniscus-engine.exe gallery-dl --help
  meniscus-engine.exe yt-dlp --help
