# Meniscus

Meniscus is a desktop front end for [gallery-dl](https://github.com/mikf/gallery-dl) and
[yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a link and it downloads the media. Everything
you download shows up in a thumbnailed, searchable library in the same window.

It is a small Python server bound to 127.0.0.1 with a plain HTML, CSS and JavaScript interface,
opened as a borderless Chrome or Chromium window. It only runs on Linux for now.

## Requirements

- Linux with an X11 or Wayland desktop
- Python 3.10 or newer
- gallery-dl, yt-dlp and Pillow (listed in `requirements.txt`)
- ffmpeg and ffprobe, for video thumbnails and Discord copies
- xclip or wl-clipboard, for the clipboard watcher
- Chrome, Chromium, Brave or Edge. Other browsers can open the page, but the refracting glass
  edges only render in Chromium-based ones.

## Install

```bash
git clone https://github.com/jasonclintford/meniscus.git
cd meniscus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The launcher uses `.venv` in the project folder when it exists and `python3` from your PATH
otherwise. Set `MENISCUS_PYTHON` to point it at a different interpreter.

Meniscus shares gallery-dl's config file. If you don't have one yet, start from the example:

```bash
mkdir -p ~/.config/gallery-dl
cp -n config.example.json ~/.config/gallery-dl/config.json
```

To get a `meniscus` command and an app menu entry:

```bash
ln -s "$PWD/meniscus" ~/.local/bin/meniscus
install -Dm644 icon.svg ~/.local/share/icons/hicolor/scalable/apps/meniscus.svg
install -Dm644 meniscus.desktop ~/.local/share/applications/meniscus.desktop
```

## Running

```bash
meniscus            # start the server if it isn't running, then open the window
meniscus --stop     # stop the server
meniscus --serve    # run the server in the foreground, for debugging
```

The server listens on `http://127.0.0.1:47821`. Set `MENISCUS_PORT` to change it. The log is at
`~/.cache/meniscus/server.log`.

## Downloading

Paste one or more links into the top bar and press Absorb. Ctrl+V anywhere in the window works
too, and so does dragging a link onto it. When you copy a link in another app, a small prompt
offers to download it. Turn on auto-absorb in Settings to skip the prompt.

Links go to gallery-dl by default, and gallery-dl hands anything it doesn't support to yt-dlp.
Click the `AUTO` chip to switch to yt-dlp mode, which runs yt-dlp directly and shows the
percentage and speed. Those downloads are saved under `<base-directory>/yt-dlp/<site>/<uploader>/`.

Running and finished downloads are listed in the Currents panel on the right. Each one fills like
a vial as it progresses.

### Sites that need a login

Instagram and a few other sites only work when you are logged in. gallery-dl reads cookies from
the browser named in `extractor.cookies`, and Meniscus passes the same setting on to yt-dlp. The
example config uses Chrome. Reading Chrome or Chromium cookies on Linux needs the `secretstorage`
package, which `requirements.txt` installs.

A 401 Unauthorized error usually means you aren't logged in to that site in the browser the config
names.

## Library

Typing anything other than a link into the top bar searches the library. Plain words match titles,
tags, creators, sites and file names. These filters narrow it down, and a leading `-` excludes a match:

| Filter | Example |
|---|---|
| Site | `site:redgifs` |
| Creator | `by:name` |
| Tag | `tag:landscape` |
| File type | `ext:mp4` |
| Loved, type or recent | `is:fav`, `is:video`, `is:image`, `is:gif`, `is:audio`, `is:new` |

The Sources view (`2`) shows every site and creator you have downloaded from, sized by how much you
hold from each. Click a site to filter the library to it.

Trash in the viewer moves the file to the desktop trash with `gio trash`, so you can restore it.

| Key | Action |
|---|---|
| `/` | Search |
| `1` / `2` | Library / Sources |
| `Q` | Show or hide the Currents panel |
| `F` | Show loved items |
| `←` `→` | Previous / next item in the viewer |
| `L` | Love the item in the viewer |

## Discord copies

Discord plays MP4 files with H.264 video and AAC audio inline on every client. VP9, HEVC and AV1
play on some clients and show up as a plain file attachment on others. Instagram and many other
sites serve VP9.

Meniscus can write a Discord-ready copy to `~/Downloads/discord-ready/` (you can change the folder
in Settings). The original file is never modified.

| Preset | Target |
|---|---|
| Discord 20 MB | Free upload limit |
| Discord 50 MB | Nitro Basic |
| Discord 500 MB | Nitro |
| Compatible MP4 | H.264 at CRF 20, no size limit |

For a single item, open it in the viewer, pick a preset under *Share-ready copy* and press
*Make copy*. Each finished copy has buttons to copy the file to the clipboard, show it in the file
manager or trash it.

To convert every new video and GIF automatically, turn on *Convert new videos* in Settings. yt-dlp
mode then also asks sites for H.264 and AAC first, so most copies only need a remux, which takes
about a second.

The encoder does as little work as it can:

1. A GIF that already fits under the limit is copied as is, because Discord autoplays GIFs and
   doesn't autoplay MP4s.
2. An H.264/AAC MP4 under the limit is remuxed without re-encoding.
3. Anything else is re-encoded with libx264. Size-limited presets use two passes and lower the
   resolution when the bitrate gets tight (1080p, 720p, 540p, 480p or 360p on the short side, and
   30 fps below 2.5 Mb/s). If the result still comes out too big, it retries at a lower bitrate.

Encodes run one at a time and appear in the Currents panel alongside downloads.

## Where files go

| What | Location |
|---|---|
| gallery-dl config (shared with the command line) | `~/.config/gallery-dl/config.json` |
| Downloads | `extractor.base-directory` from that config, default `~/Downloads/gdl` |
| Download archive, so files aren't fetched twice | `extractor.archive` from that config |
| Metadata sidecars (tags, titles, source URL) | `.meta/` next to each file |
| Library index and download history | `~/.local/share/meniscus/library.sqlite3` |
| Thumbnails | `~/.cache/meniscus/thumbs` |
| Settings | `~/.config/meniscus/settings.json` |

## How the glass is drawn

Each glass panel has three layers. The first is a backdrop blur with extra saturation. The second
is an SVG displacement map generated per panel from the distance to its rounded edge, so the
middle stays clear and only the rim bends what is behind it. The red, green and blue channels are
displaced by slightly different amounts, which gives the rim a faint prismatic fringe. Chrome is
the only browser that applies SVG filters to `backdrop-filter`, so this layer is skipped elsewhere.
The third layer is a highlight that follows the pointer, plus a rim light that turns toward it.

The background colours come from your media. Four blurred blobs behind the interface take the
dominant colours of whatever is on screen, and opening an item in the viewer tints the whole
window to match it.

## License

MIT. See [LICENSE](LICENSE).
