# OpenScan Multicam Camera Node MVP

This repository is a small MVP for provisioning 2-3 Raspberry Pi Zero 2 camera nodes and controlling them from a developer laptop.

The shape is deliberately simple:

- Ansible provisions Raspberry Pi OS Lite hosts over SSH.
- Each node runs a native Python FastAPI service under systemd.
- The service starts and stops `rpicam-vid` as a subprocess.
- Recordings are written under `/srv/openscan-camera/sessions/<session_id>/<camera_id>/<take_id>/`.
- `/srv/openscan-camera` is shared over Samba.
- A small coordinator CLI sends concurrent HTTP requests to all configured nodes.
- The coordinator can harvest a completed distributed session into one local archive.

There is no Docker, no Swarm, no final OpenScan3 web UI, and no video postprocessing in this MVP.

## Repository Layout

```text
ansible/       Raspberry Pi provisioning playbook, dynamic inventory, and roles
camera_node/   FastAPI camera-node HTTP service
coordinator/   multicam CLI for controlling nodes
examples/      recording profile config
multivid.yml   local fleet configuration (not committed)
```

## Starting Point

Start with:

- A fresh Raspberry Pi OS Lite image on each SD card.
- SSH enabled on each Pi.
- Hostnames or IP addresses known from your network.
- A camera connected to each Pi and confirmed working.
- A developer laptop with Ansible installed.

Current Raspberry Pi OS Lite includes the `rpicam-apps-lite` package, which provides `rpicam-vid` and the still-image tools used for positioning/reference JPEGs. The Ansible defaults also ensure that package is present.

For the Arducam IMX519 modules used by this MVP, Ansible also writes these lines into the Raspberry Pi boot config and reboots the node when needed:

```text
camera_auto_detect=0
dtoverlay=imx519
```

On Raspberry Pi OS Bookworm this is `/boot/firmware/config.txt`; on older images it is `/boot/config.txt`.

The example config also enables Arducam's Pivariety installer with `openscan_arducam_pivariety_install: true` so IMX519 autofocus support is installed. This downloads Arducam's installer script on each Pi and installs the `libcamera_dev` and `libcamera_apps` packages documented by Arducam for Bookworm/Bullseye systems.

## Provisioning Flow

1. Flash SD cards with Raspberry Pi OS Lite.
2. Boot the Pis and let them join the network.
3. Confirm SSH access from your laptop:

   ```bash
   ssh user@cam-front.local
   ```

4. Copy and edit the fleet configuration:

   ```bash
   cp multivid.example.yml multivid.yml
   $EDITOR multivid.yml
   ```

   It contains the bootstrap SSH user, private key and node hosts. HTTP URLs, harvest user, ports and service paths are derived defaults.

5. Run the playbook from the repository root:

   ```bash
   ansible-playbook ansible/playbooks/site.yml
   ```

6. Check camera-node health:

   ```bash
   curl http://cam-front.local:8080/health
   curl http://cam-side.local:8080/status
   ```

7. Install the coordinator CLI on your laptop:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e coordinator
   ```

8. Start a test recording:

   ```bash
   multicam --config multivid.yml status
   multicam --config multivid.yml profiles
   multicam --config multivid.yml start --session test-001 --profile video_1080p25_auto
   multicam --config multivid.yml stop
   ```

9. Harvest the distributed recording into one local session folder:

   ```bash
   multicam --config multivid.yml harvest --session test-001 --output ./harvested_sessions
   ```

10. Access recordings manually through Samba when needed:

   ```text
   smb://cam-front.local/openscan-camera
   ```

## Camera Node API

Each node exposes:

- `GET /health`
- `GET /status`
- `GET /profiles`
- `GET /sessions`
- `GET /sessions/{session_id}`
- `GET /sessions/{session_id}/takes`
- `GET /sessions/{session_id}/manifest-summary`
- `POST /recordings/start`
- `POST /recordings/stop`
- `POST /prepare`
- `POST /prepare/reset`
- `POST /calibration/run`
- `GET /calibration/status`
- `GET /calibration/last`
- `POST /calibration/apply-to-session`
- `POST /positioning/start`
- `POST /positioning/stop`
- `GET /positioning/status`
- `GET /positioning/snapshot.jpg`
- `GET /positioning/stream.mjpg`
- `POST /stills/capture`
- `GET /stills/status`

Start requests use this JSON body:

```json
{
  "session_id": "test-001",
  "profile": "video_1080p25_auto",
  "take_id": "take_001",
  "force_prepare": false,
  "refocus": false,
  "apply_calibration_suggestions": false,
  "notes": "optional operator note"
}
```

Only `session_id` and `profile` are required. If `take_id` is omitted, the node creates the next available `take_001`, `take_002`, and so on.

Prepare requests use this JSON body:

```json
{
  "session_id": "test-001",
  "profile": "video_1080p25_auto",
  "force": false,
  "refocus": false
}
```

Prepare reset requests use this JSON body:

```json
{
  "session_id": "test-001"
}
```

Calibration run requests use this JSON body:

```json
{
  "session_id": "test-001",
  "profile": "video_1080p25_auto",
  "duration_seconds": 5,
  "calibration_id": "cal-gray-card",
  "target": "gray_card",
  "notes": "optional operator note",
  "apply_to_session": false
}
```

Only `session_id` and `profile` are required. The node writes calibration output under:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/calibration/<calibration_id>/
```

That directory contains `calibration_manifest.json`, `suggested_controls.json`, `rpicam-vid.stderr.log`, and metadata or preview files when the backend can produce them.

The service loads:

- `/etc/openscan-camera-node/config.yaml`
- `/etc/openscan-camera-node/profiles.yaml`

`config.yaml` contains the node `camera_id` and may also contain `profile_overrides` for camera-specific values such as AWB gains and lens position. It also has conservative calibration policy defaults:

```yaml
camera_control_policy:
  use_calibration_suggestions: false
  apply_suggestions_to_recording: false
positioning:
  width: 640
  height: 360
  fps: 5
  jpeg_quality: 75
  overlays:
    - camera_label
    - crosshair
    - shorts_safe_area
reference_stills:
  quality: 95
  width: null
  height: null
  use_recording_profile_controls: true
```

Leave `apply_suggestions_to_recording` false unless you deliberately want the node to pass suggested lock values to `rpicam-vid`.

The prepared state for a session/camera is written to:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/prepared_state.json
```

The manifest for each take is written to:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/<take_id>/manifest.json
```

`rpicam-vid` stderr is captured next to the recording as `rpicam-vid.stderr.log`.

## Positioning preview

Positioning preview is a low-resolution, low-framerate setup mode for physically aligning camera nodes before recording. It is intentionally not a final recording mode and not a high-quality livestream.

Start preview across all configured nodes:

```bash
multicam --config multivid.yml positioning-start --overlay crosshair --overlay shorts-safe-area
```

Useful options:

```bash
multicam --config multivid.yml positioning-start --width 640 --height 360 --fps 5
multicam --config multivid.yml positioning-status
multicam --config multivid.yml positioning-urls
```

`positioning-urls` prints browser-openable URLs for each node:

```text
http://cam-front.local:8080/positioning/snapshot.jpg
http://cam-front.local:8080/positioning/stream.mjpg
```

Supported overlays are:

- `camera-label`
- `crosshair`
- `grid`
- `shorts-safe-area`

The node enters the `positioning` state while preview is active. Recording, calibration, and reference still capture are rejected while positioning is active. Stop preview before recording:

```bash
multicam --config multivid.yml positioning-stop
```

## Reference stills

Reference stills are high-quality JPEGs for focus, framing, lighting, and 9:16 crop checks before a real take. They are not scan images and are not recording takes.

Capture one still per node:

```bash
multicam --config multivid.yml stills-capture --session test-001 --label alignment_001
```

Optional controls:

```bash
multicam --config multivid.yml stills-capture --session test-001 --label alignment_001 --profile video_1080p25_locked
multicam --config multivid.yml stills-status
```

Each node stores reference stills under:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/reference_stills/
```

Example:

```text
reference_stills/
  alignment_001.jpg
  alignment_001_manifest.json
```

The still manifest records session, camera, label, timestamp, hostname, service version, output path, requested size/quality, actual file size, backend command, optional profile/control snapshot, warnings, errors, and the state before capture.

For safety, the MVP rejects reference still capture while recording, calibrating, or positioning. If a label already exists, the node generates a unique suffix such as `alignment_001_002` unless `force` is explicitly requested through the API/CLI.

## Recommended setup workflow

1. Boot nodes.
2. Run `multicam positioning-start`.
3. Open preview URLs from `multicam positioning-urls`.
4. Physically align cameras.
5. Stop positioning with `multicam positioning-stop`.
6. Capture reference stills with `multicam stills-capture --session <session_id> --label alignment_001`.
7. Check focus, framing, lighting, and 9:16 crop suitability.
8. Start the recording session.
9. Stop recording.
10. Harvest the session.

## Minimal Operator Dashboard

Milestone 4.6 adds a small host-side browser dashboard for local capture operation. It runs on the coordinator/laptop, not on each camera node. The browser calls the dashboard backend, and the dashboard backend calls the existing camera-node APIs concurrently.

Start it from the repository environment where the coordinator package is installed:

```bash
multicam --config multivid.yml dashboard --port 8090
```

Then open:

```text
http://localhost:8090
```

The command prints the dashboard URL, loaded node count, config path, and a warning that this is a local/trusted-network MVP with no authentication.

The dashboard is optimized for local laptop/desktop operation at normal browser zoom. It is intended to keep two or three camera previews visible and operable without zooming out on typical 1366px, 1440px, and 1920px wide screens.

Preview snapshots and MJPEG streams are proxied through the dashboard backend. Browser clients such as tablets only need to reach the coordinator dashboard URL; they do not need to resolve camera-node hostnames directly.

The dashboard shows all configured camera nodes on one page and can:

- refresh node status while tolerating offline nodes
- start and stop low-resolution positioning preview
- show MJPEG preview streams or periodically refreshed snapshots
- capture high-resolution reference stills across nodes
- run calibration across nodes for inspection
- start and stop recordings across nodes
- show per-node success, rejection, offline, warning, and error messages

Example dashboard workflow:

1. Start dashboard: `multicam --config multivid.yml dashboard --port 8090`
2. Open `http://localhost:8090`.
3. Enter `session_id`.
4. Start positioning.
5. Align cameras using the preview cards.
6. Stop positioning.
7. Capture reference stills.
8. Run calibration if needed.
9. Start recording.
10. Stop recording.
11. Harvest from the CLI for now.

Dashboard and positioning defaults are built into the application. Override positioning settings per command when required.

The dashboard respects node-side state validation. It does not force-stop positioning before recording, does not apply calibration suggestions as part of calibration capture, and does not hide partial failures. Recording profiles still decide whether linked calibration suggestions are applied to `rpicam-vid`.

### What this dashboard intentionally does not do yet

- No clip factory.
- No render/export.
- No ffmpeg rendering.
- No Kdenlive integration.
- No timeline.
- No session gallery.
- No user authentication.
- No cloud access.
- No final OpenScan3 UI integration.

### Manual dashboard verification checklist

- Start dashboard with two or three configured nodes.
- Open the dashboard at 100% browser zoom.
- Confirm top controls use less vertical space than the earlier MVP layout.
- Confirm all node cards appear.
- Confirm three camera cards fit in one row on a 1920px wide screen.
- Confirm the layout remains usable around 1440x900 and 1366px wide.
- Start positioning from the dashboard.
- Confirm streams or snapshots appear and previews remain visible without zooming out.
- Stop positioning and confirm stopped-preview placeholders do not dominate the cards.
- Confirm controls remain usable after wrapping.
- Confirm operation results are readable.
- Stop positioning.
- Capture reference stills.
- Run calibration.
- Start recording.
- Stop recording.
- Confirm per-node errors are visible if one node is offline.
- Confirm all existing dashboard buttons still work.

## Harvesting a session

Recording happens on the camera nodes. Harvesting is the next step: the coordinator collects each node's session files into one central folder so later rendering/editing tools have a reproducible local archive to consume.

The first harvesting backend is `rsync_ssh`. Samba remains useful for manual browsing, but the harvester gives a repeatable command, a stable folder structure, and machine-readable `session_index.json` plus `harvest_report.json`.

Example workflow:

```bash
multicam --config multivid.yml start --session benchy_scan_001 --profile video_1080p25_locked
multicam --config multivid.yml stop
multicam --config multivid.yml harvest --session benchy_scan_001 --output ./harvested_sessions
```

Inspect:

```text
harvested_sessions/benchy_scan_001/session_index.json
harvested_sessions/benchy_scan_001/harvest_report.json
```

The harvested structure is:

```text
harvested_sessions/
  benchy_scan_001/
    session_index.json
    harvest_report.json
    nodes/
      front/
        prepared_state.json
        take_001/
          recording.h264
          manifest.json
          rpicam-vid.stderr.log
        reference_stills/
          alignment_001.jpg
          alignment_001_manifest.json
      side/
        prepared_state.json
        take_001/
          recording.h264
          manifest.json
          rpicam-vid.stderr.log
        reference_stills/
          alignment_001.jpg
          alignment_001_manifest.json
```

Harvesting copies `reference_stills/*.jpg` and `reference_stills/*_manifest.json`. `session_index.json` includes `reference_stills` per node with label, file paths, timestamp, manifest summary, warnings, and errors. Reference stills are indexed separately and are not treated as recording takes.

Harvesting uses the same `multivid.yml` connection and node definitions:

```yaml
connection:
  bootstrap_user: user
  identity_file: ~/.ssh/id_ed25519
nodes:
  front:
    host: cam-front.local
```

The coordinator derives `http://HOST:8080`, the `openscan` harvest user and `/srv/openscan-camera/sessions`; it explicitly passes `identity_file` to `rsync`.

Ansible provisions this SSH harvest path by default:

- Installs `rsync` on camera nodes.
- Gives the `openscan` service user a login shell for key-based SSH harvesting.
- Uses `/var/lib/openscan-camera` as the `openscan` home so SSH `authorized_keys` is not placed in the group-writable Samba share.
- Installs the public key paired with `connection.identity_file`.

If that key does not exist yet, create it before running Ansible:

```bash
ssh-keygen -t ed25519
ansible-playbook ansible/playbooks/site.yml -K
```

Use `-k` only when Ansible should connect over SSH password instead of your existing SSH key. Password SSH requires `sshpass` on the coordinator.

After provisioning, this should work without a password prompt:

```bash
ssh -i ~/.ssh/id_ed25519 openscan@cam-front.local 'ls -la /srv/openscan-camera/sessions'
```

Useful options:

```bash
multicam --config multivid.yml harvest --session benchy_scan_001 --dry-run
multicam --config multivid.yml harvest --session benchy_scan_001 --node cam-front
multicam --config multivid.yml harvest --session benchy_scan_001 --overwrite
multicam --config multivid.yml harvest --session benchy_scan_001 --allow-partial
multicam --config multivid.yml harvest --session benchy_scan_001 --hash-video
```

Harvesting is idempotent. If a local file already exists and the remote size and mtime match, it is reported as unchanged. If a local file exists with different metadata, the harvester does not overwrite it unless `--overwrite` is set. Offline nodes, missing sessions, missing manifests, missing recordings, empty recordings, and manifest identity mismatches are recorded in `harvest_report.json` and `session_index.json`. The command returns a non-zero exit code for incomplete harvests unless `--allow-partial` is set.

## What harvesting intentionally does not do yet

- No video rendering.
- No timelapse generation.
- No split-screen output.
- No rendered video overlays.
- No shorts.
- No editor timeline export.

## Generating a multicam review stringout

The multicam review stringout is the first derived video artifact after harvesting. It is a review file, not a finished marketing edit. Use it to quickly check whether cameras recorded, framing/focus/exposure are acceptable, angles are useful, and takes are worth editing.

The stringout generator reads a harvested session folder, uses `session_index.json` to discover takes and camera recordings, trims pre-roll when metadata is available, renders each take as a simple multicam grid, adds short take slates, concatenates the takes, and writes a debugging report with warnings and ffmpeg commands.

Default output:

```text
harvested_sessions/<session_id>/derivatives/review/
  multicam_stringout.mp4
  multicam_stringout_report.json
  ffmpeg_commands.txt
  takes/
    take_001_multicam_stringout.mp4
    take_002_multicam_stringout.mp4
  logs/
```

By default the command creates both one full-session stringout and one review file per take. The per-take files are useful for notes such as "use the side camera around take_003 02:15". Pass `--no-per-take` to render only the full-session stringout.

Default render settings are 1920x1080, 30 fps, 5x speed, H.264 via `libx264`, `yuv420p`, and no audio. The MVP supports 1, 2, 3, and 4 camera grids. Three cameras render as a 2x2 grid with one empty black tile. Each grid has camera labels plus a centered take/time overlay showing usable source-take time after pre-roll. If camera label, timecode, or slate text rendering fails because `ffmpeg drawtext` is unavailable, the renderer retries without that text and records a warning.

Example workflow:

1. Start dashboard:

   ```bash
   multicam --config multivid.yml dashboard --port 8090
   ```

2. Position cameras and record one or more takes.
3. Harvest:

   ```bash
   multicam --config multivid.yml harvest --session benchy_scan_001 --output ./harvested_sessions
   ```

4. Generate stringout:

   ```bash
   multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --speed 5
   ```

5. Open:

   ```text
   ./harvested_sessions/benchy_scan_001/derivatives/review/multicam_stringout.mp4
   ```

Useful options:

```bash
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --dry-run
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --take take_001
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --include-cameras front,side,top
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --no-per-take
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --realtime
multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001 --overwrite
```

With `--take take_001`, the default output is only the matching per-take file under `derivatives/review/takes/`. Add `--no-per-take` if you want the older single-output behavior for a selected take. The command does not overwrite existing full-session or per-take outputs unless `--overwrite` is passed. `--dry-run` parses the session index, resolves takes/cameras/files, prints the planned output, and does not call `ffmpeg` or write video/report files.

## Preparing edit assets

Node recordings are raw `.h264` capture masters. For manual editing, the coordinator can remux those recordings into MP4 containers that are easier to import, inspect, and organize in Kdenlive or another NLE. The default master generation uses `ffmpeg -c copy`, so it does not re-encode and should not reduce quality.

Default output:

```text
harvested_sessions/<session_id>/edit_assets/
  masters_mp4/
    front_take_001.mp4
    side_take_001.mp4
    top_take_001.mp4
  proxies/
    front_take_001_proxy.mp4
  edit_assets_index.json
  edit_assets_index.csv
  edit_assets_report.json
  kdenlive_import_notes.md
  import_list.txt
```

Proxy generation is optional because Kdenlive can also generate proxies itself. When `--proxies` is used, the coordinator writes low-bitrate H.264 MP4 proxy files under `edit_assets/proxies/`.

Example workflow:

1. Record a session.
2. Harvest:

   ```bash
   multicam harvest --session benchy_scan_001 --output ./harvested_sessions
   ```

3. Generate a review stringout:

   ```bash
   multicam derive-stringout --session-path ./harvested_sessions/benchy_scan_001
   ```

4. Prepare edit assets:

   ```bash
   multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001
   ```

5. Open Kdenlive.
6. Import:
   - `edit_assets/masters_mp4/`
   - `derivatives/review/multicam_stringout.mp4`
   - reference stills from the harvested session if useful

Useful options:

```bash
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --dry-run
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --proxies
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --proxy-height 540 --proxies
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --include-cameras front,side,top
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --take take_001
multicam prepare-edit-assets --session-path ./harvested_sessions/benchy_scan_001 --overwrite
```

The command does not overwrite existing MP4 masters or proxies unless `--overwrite` is passed. `--dry-run` parses `session_index.json`, resolves recordings, shows planned outputs, and does not call `ffmpeg` or write video/report files.

### What this milestone intentionally does not do

- No Kdenlive project generation.
- No automatic timeline.
- No edit decisions.
- No final rendering.
- No Shorts generation.
- No clip selection.
- No color correction.
- No audio workflow.

### What the stringout milestone intentionally does not do

- No final edit.
- No Shorts.
- No Kdenlive project.
- No clip selection.
- No automatic best-angle decisions.
- No fancy graphics.
- No full recipe system.
- No dashboard integration.

## Reliable Take Lifecycle

The camera-node service tracks a simple lifecycle:

- `idle`
- `preparing`
- `armed`
- `positioning`
- `recording`
- `stopping`
- `completed`
- `error`

Normal user flow:

1. Run `multicam start --session <id> --profile <profile>`.
2. The first start for a session automatically prepares each camera.
3. The node starts recording immediately and marks the first configured seconds as pre-roll in the manifest.
4. Later takes in the same session reuse prepared state when the session, profile, and relevant camera config still match.
5. Use `--force-prepare` if lighting, framing, or the scene materially changed.
6. Use `--refocus` if object distance changed.
7. Use a new session for a materially different setup.

During prepare, the MVP validates the selected profile, creates/checks output paths, records disk space, records the requested camera-control policy, and optionally runs a short `rpicam-vid` warmup when `prepare_warmup_seconds` is greater than zero. The normal coordinator path does not need to call prepare explicitly, but these debugging commands are available:

```bash
multicam --config multivid.yml prepare --session test-001 --profile video_1080p25_auto
multicam --config multivid.yml prepare --session test-001 --profile video_1080p25_auto --force
multicam --config multivid.yml prepare-reset --session test-001
```

Status output includes state, backend, prepared session/profile validity, recording session/take/profile, output path, PID, last error, free disk space, service version, resolved controls, applied controls, unsupported controls, and warnings.

Status output also includes `positioning_running`, active positioning settings and preview paths, `last_positioning_error`, `last_still_capture`, and `allowed` booleans for recording, positioning, calibration, and still capture.

Each take manifest includes the session, take, camera, hostname, service version, profile snapshot after node overrides, prepared-state snapshot, requested controls, resolved controls, controls actually passed to `rpicam-vid`, unsupported controls, start/stop times, pre-roll seconds, usable start offset/time, output file path, full command, PID, exit code, warnings, and errors.

## Recording Profiles

The default profiles live in `examples/profiles.yml` and are installed onto each node by Ansible:

- `video_1080p25_auto`
- `video_1080p25_locked`
- `video_1080p25_calibrated_suggest`
- `video_1080p25_auto_then_lock_experimental`
- `video_1080p50_experimental_locked`
- `timelapse_1080p6_locked`

Profiles use structured `recording`, `camera_controls`, and `camera_control_policy` blocks. The service translates known structured values into `rpicam-vid` arguments, then appends `--output <file>` and `--timeout 0` unless the profile already provides those options. Use `rpicam_vid_extra_args` for advanced flags that are not modeled yet.

The default profiles write `recording.h264` files by using the `rpicam-vid` hardware H.264 encoder:

```yaml
output_extension: h264
recording:
  codec: h264
rpicam_vid_extra_args:
  - --inline
```

The `.h264` extension is intentional: `rpicam-vid --codec h264` writes a raw H.264 elementary stream, not an MP4 container. For this MVP the nodes prioritize reliable capture with minimal muxing overhead. Remux to MP4 later on a stronger machine when needed, without re-encoding the video stream.

Do not set `output_extension: mp4` on a raw `--codec h264` profile. That produces a mislabeled raw H.264 bitstream, not an MP4 container. MP4 should only be used for an explicit container profile, for example with `--codec libav` and an MP4 libav format.

Profiles also support `camera_control_policy`:

```yaml
camera_control_policy:
  pre_roll_seconds: 5
  exposure_mode: auto
  awb_mode: auto_then_lock
  focus_mode: auto
  reuse_prepared_controls: true
  refocus_on_each_take: false
  prepare_warmup_seconds: 0
```

For multicam consistency, AE/AWB should ideally warm up and then lock. With the current `rpicam-vid` subprocess backend, this is only a partial foundation: calibration can capture metadata when available, prepare can link suggestions, and profiles such as `video_1080p25_calibrated_suggest` can apply available suggestions during recording. Only values observed in metadata are applied. The metadata is honest about gaps with warnings.

Avoid continuous autofocus for final takes unless it is explicitly needed. The `video_1080p50_experimental_locked` profile is for testing only.

## Calibration and suggested locks

Calibration runs are short `rpicam-vid` captures used to collect backend metadata and suggest stable manual values for later profiles or per-node overrides. With the current `rpicam-vid` backend, not all values may be available. The service only suggests values it actually observed in metadata. Missing values are written as `null` with warnings such as `Not available from rpicam-vid metadata on this backend`.

Run a calibration pass across all configured nodes:

```bash
multicam --config multivid.yml calibrate --session test-001 --profile video_1080p25_auto
multicam --config multivid.yml calibrate --session test-001 --profile video_1080p25_auto --duration 8
multicam --config multivid.yml calibration-status
multicam --config multivid.yml calibration-last
multicam --config multivid.yml calibration-suggestions
```

Recommended workflow:

1. Start with an auto profile.
2. Place a gray card or representative scene in view.
3. Run `multicam calibrate`.
4. Review suggested values and warnings.
5. For direct calibrated capture, record with `video_1080p25_calibrated_suggest`.
6. For a conservative permanent setup, copy good values into per-node overrides and use a locked profile for real takes.

Example override snippet:

```yaml
profile_overrides:
  video_1080p25_locked:
    camera_controls:
      shutter_us: 10000
      gain: 1.7
      awbgains: [1.82, 1.41]
      lens_position: 1.8
```

Suggestions are not automatically written back into `profiles.yaml` or Ansible host vars. `multicam calibration-suggestions` prints copyable snippets, but you decide what to keep. The `video_1080p25_calibrated_suggest` profile is the no-host-edit path: it links available calibration suggestions for the session and applies them during recording.

Profiles can link and apply suggestions during prepare/recording:

```yaml
camera_control_policy:
  exposure_mode: auto_then_lock
  awb_mode: auto_then_lock
  focus_mode: auto_then_lock
  use_calibration_suggestions: true
  apply_suggestions_to_recording: true
```

If suggestions are available and `reuse_prepared_controls` is true, `prepared_state.json` records the calibration id, manifest path, suggestion path, and a snapshot of the suggested controls. `video_1080p25_calibrated_suggest` applies those available suggestions automatically because its profile policy sets `apply_suggestions_to_recording: true`:

```bash
multicam --config multivid.yml calibrate --session test-001 --profile video_1080p25_auto --apply-to-session
multicam --config multivid.yml start --session test-001 --profile video_1080p25_calibrated_suggest
```

The explicit `--apply-calibration-suggestions` start flag remains available for testing other profiles that link suggestions but do not apply them by default.

Each take manifest records whether suggestions were linked and whether they were actually applied. `auto_then_lock` remains experimental until a Picamera2 backend can read and apply AE/AWB/AF controls reliably.

## Deterministic camera controls

Auto profiles are useful for quick tests. Locked profiles are recommended for multicam consistency because each node receives explicit `rpicam-vid` arguments for the controls configured in the resolved profile.

For consistent colors across cameras, set manual `awbgains`. For consistent motion blur, set fixed `shutter_us`. For consistent brightness and noise, set fixed `gain`. For fixed camera setups, avoid continuous autofocus and use `autofocus_mode: manual` with a per-node `lens_position`.

For physically mirrored or upside-down camera mounts, set node-level `camera_transform`. These flags are applied consistently to recordings, calibration captures, positioning previews, and reference stills, independent of the selected recording profile:

```yaml
camera_transform:
  hflip: true
  vflip: false
```

Per-node overrides are expected because camera modules and physical angles differ. Ansible renders these into `/etc/openscan-camera-node/config.yaml`:

```yaml
profile_overrides:
  video_1080p25_locked:
    camera_controls:
      awbgains: [1.75, 1.42]
      lens_position: 1.8
```

The resolved profile snapshot is stored in each `manifest.json`. `requested_controls` records the profile intent, `resolved_controls` records the values after node overrides, and `applied_controls` only records controls that the service actually passed as `rpicam-vid` arguments. Unknown or future fields are preserved under `unsupported_controls` with warnings instead of being silently treated as applied.

## What this MVP intentionally does not do yet

- No Docker or Docker Swarm.
- No final OpenScan3 web UI.
- No automatic dashboard-driven harvesting from camera nodes.
- No ffmpeg postprocessing or rendering.
- No video editing workflow.
- No complicated synchronization.
- No camera discovery.
- No production secret handling beyond documenting where Ansible Vault should be used.

## What Milestone 4.5 intentionally does not do

- No high-quality livestreaming.
- No recording while positioning.
- No automatic camera angle scoring.
- No automatic focus scoring.
- No still gallery UI.
- No video rendering.
- No clip factory or stringout generation.

## Next steps

- Add `start_at` synchronization for closer multi-node start timing.
- Add a Picamera2 backend that can actually measure and lock AE/AWB/AF values during prepare.
- Add richer hardware health checks.
- Add OpenScan3 integration points after the camera-node lifecycle is stable.
