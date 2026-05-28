# OpenScan Multicam Camera Node MVP

This repository is a small MVP for provisioning 2-3 Raspberry Pi Zero 2 camera nodes and controlling them from a developer laptop.

The shape is deliberately simple:

- Ansible provisions Raspberry Pi OS Lite hosts over SSH.
- Each node runs a native Python FastAPI service under systemd.
- The service starts and stops `rpicam-vid` as a subprocess.
- Recordings are written under `/srv/openscan-camera/sessions/<session_id>/<camera_id>/<take_id>/`.
- `/srv/openscan-camera` is shared over Samba.
- A small coordinator CLI sends concurrent HTTP requests to all configured nodes.

There is no Docker, no Swarm, no web UI, no harvesting, and no video postprocessing in this MVP.

## Repository Layout

```text
ansible/       Raspberry Pi provisioning playbook, inventory, and roles
camera_node/   FastAPI camera-node HTTP service
coordinator/   multicam CLI for controlling nodes
examples/      example node and recording profile config
```

## Starting Point

Start with:

- A fresh Raspberry Pi OS Lite image on each SD card.
- SSH enabled on each Pi.
- Hostnames or IP addresses known from your network.
- A camera connected to each Pi and confirmed working.
- A developer laptop with Ansible installed.

Current Raspberry Pi OS Lite includes the `rpicam-apps-lite` package, which provides `rpicam-vid`. The Ansible defaults also ensure that package is present.

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
   ssh pi@cam-front.local
   ```

4. Copy and edit the inventory:

   ```bash
   cp ansible/inventory.example.yml ansible/inventory.yml
   $EDITOR ansible/inventory.yml
   ```

   Update `ansible_host`, `ansible_user`, and each `camera_id`. If you prefer host var files, copy the examples in `ansible/host_vars/*.example.yml` to matching `.yml` files.

5. Set the Samba password in `ansible/group_vars/camera_nodes.yml`.

   For quick MVP testing the example config enables anonymous Samba access with `openscan_samba_guest_access: true`. Set that to `false` to require the configured Samba user and password. If you store a real password there, use Ansible Vault.

6. Run the playbook from the repository root:

   ```bash
   ansible-playbook -i ansible/inventory.yml ansible/playbooks/site.yml
   ```

7. Check camera-node health:

   ```bash
   curl http://cam-front.local:8080/health
   curl http://cam-side.local:8080/status
   ```

8. Install the coordinator CLI on your laptop:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e coordinator
   ```

9. Edit `examples/nodes.yml`, then start a test recording:

   ```bash
   multicam --nodes examples/nodes.yml status
   multicam --nodes examples/nodes.yml profiles
   multicam --nodes examples/nodes.yml start --session test-001 --profile video_1080p25_auto
   multicam --nodes examples/nodes.yml stop
   ```

10. Access recordings through Samba:

   ```text
   smb://cam-front.local/openscan-camera
   ```

## Camera Node API

Each node exposes:

- `GET /health`
- `GET /status`
- `GET /profiles`
- `POST /recordings/start`
- `POST /recordings/stop`
- `POST /prepare`
- `POST /prepare/reset`

Start requests use this JSON body:

```json
{
  "session_id": "test-001",
  "profile": "video_1080p25_auto",
  "take_id": "take_001",
  "force_prepare": false,
  "refocus": false,
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

The service loads:

- `/etc/openscan-camera-node/config.yaml`
- `/etc/openscan-camera-node/profiles.yaml`

`config.yaml` contains the node `camera_id` and may also contain `profile_overrides` for camera-specific values such as AWB gains and lens position.

The prepared state for a session/camera is written to:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/prepared_state.json
```

The manifest for each take is written to:

```text
/srv/openscan-camera/sessions/<session_id>/<camera_id>/<take_id>/manifest.json
```

`rpicam-vid` stderr is captured next to the recording as `rpicam-vid.stderr.log`.

## Reliable Take Lifecycle

The camera-node service tracks a simple lifecycle:

- `idle`
- `preparing`
- `armed`
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
multicam --nodes examples/nodes.yml prepare --session test-001 --profile video_1080p25_auto
multicam --nodes examples/nodes.yml prepare --session test-001 --profile video_1080p25_auto --force
multicam --nodes examples/nodes.yml prepare-reset --session test-001
```

Status output includes state, backend, prepared session/profile validity, recording session/take/profile, output path, PID, last error, free disk space, service version, resolved controls, applied controls, unsupported controls, and warnings.

Each take manifest includes the session, take, camera, hostname, service version, profile snapshot after node overrides, prepared-state snapshot, requested controls, resolved controls, controls actually passed to `rpicam-vid`, unsupported controls, start/stop times, pre-roll seconds, usable start offset/time, output file path, full command, PID, exit code, warnings, and errors.

## Recording Profiles

The default profiles live in `examples/profiles.yml` and are installed onto each node by Ansible:

- `video_1080p25_auto`
- `video_1080p25_locked`
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

For multicam consistency, AE/AWB should ideally warm up and then lock. With the current `rpicam-vid` subprocess backend, AE/AWB/AF lock values are not measured or applied by the service yet. The metadata is honest about this: for example, a requested `auto_then_lock` AWB policy is recorded as requested, but the actually applied backend behavior is recorded as `auto` with a warning.

Avoid continuous autofocus for final takes unless it is explicitly needed. The `video_1080p50_experimental_locked` profile is for testing only.

## Deterministic camera controls

Auto profiles are useful for quick tests. Locked profiles are recommended for multicam consistency because each node receives explicit `rpicam-vid` arguments for the controls configured in the resolved profile.

For consistent colors across cameras, set manual `awbgains`. For consistent motion blur, set fixed `shutter_us`. For consistent brightness and noise, set fixed `gain`. For fixed camera setups, avoid continuous autofocus and use `autofocus_mode: manual` with a per-node `lens_position`.

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
- No web UI.
- No automatic harvesting from camera nodes.
- No ffmpeg postprocessing or rendering.
- No video editing workflow.
- No complicated synchronization.
- No camera discovery.
- No production secret handling beyond documenting where Ansible Vault should be used.

## Next steps

- Add `start_at` synchronization for closer multi-node start timing.
- Add a Picamera2 backend that can actually measure and lock AE/AWB/AF values during prepare.
- Add richer hardware health checks.
- Add OpenScan3 integration points after the camera-node lifecycle is stable.
