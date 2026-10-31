# GPU Tuner on Unraid

GPU Tuner provides NVIDIA GPU monitoring, power limits, fan curves and clock caps through a password-protected WebUI. This Unraid package is maintained in [0sy/gpu-tuner-unraid](https://github.com/0sy/gpu-tuner-unraid), based on [willboyden/gpu-tuner](https://github.com/willboyden/gpu-tuner).

## Requirements

- Unraid with Docker enabled and the NVIDIA Driver plugin installed.
- A supported NVIDIA GPU visible in `nvidia-smi`. A GPU assigned exclusively to a VM is unavailable to the container.
- A dedicated writable appdata directory. The default is `/mnt/user/appdata/gpu-tuner`.
- An unused host port for the WebUI. The default is TCP 8765.

Some GPU models support monitoring without exposing fan or clock controls. Use the WebUI on a trusted LAN. The container requires GPU access and `SYS_ADMIN` for GPU control; the supplied template configures these permissions without enabling privileged mode.

## Installation

1. In **Apps**, search for **GPU Tuner** and select Install. If the listing is not yet available, use the manual template instructions below.
2. Set **Appdata** to a dedicated directory, mapped to `/data`.
3. Choose the host **WebUI Port**.
4. Enter **WebUI URL** using your Unraid server's IPv4 address and chosen host port, for example `http://192.168.1.10:8765`. Include the explicit port and no path. Both the value and default are intentionally blank in the distributed template.
5. Enter an **Initial WebUI Password** of 12–1024 characters. This masked field is mandatory; there is no default password.
6. Retain the GPU parameters and 45-second stop timeout, then select **Apply**.
7. Open **WebUI** from the container's context menu and sign in.
8. Edit the container, enable **Advanced View**, and use **Remove** on the **Initial WebUI Password** variable. Select **Apply**, then sign in with the same password.

Remove the initial-password variable entirely rather than blanking its required field. Your password hash remains in appdata. Removing the variable removes the plaintext from the current Docker configuration and saved template; installation logs and earlier backups may still contain it. An existing password hash is never replaced just by setting the initial-password variable again.

Run only one GPU controller against the same cards at a time. Controls in the WebUI change settings on your real GPUs.

### Manual template installation

The public image is `ghcr.io/0sy/gpu-tuner-unraid:latest`. Versioned tags are also available, including `1.3.1-unraid.1-beta.1`.

Download [gpu-tuner.xml](https://raw.githubusercontent.com/0sy/gpu-tuner-unraid/main/templates/gpu-tuner.xml) and save it on Unraid as:

```text
/boot/config/plugins/dockerMan/templates-user/my-gpu-tuner.xml
```

Open **Docker → Add Container**, select the GPU Tuner template, and complete the installation settings above. Keep the image repository set to the public GHCR image.

## Networking

The default template uses **bridge** networking and maps your chosen host port to container port 8765. If you change the host port, update **WebUI URL** to match.

For a custom network, assign an unused container IPv4 address in Unraid and set WebUI URL to that address with port 8765. Host networking is unsupported: startup expects one non-loopback container IPv4 address.

The WebUI URL must match the address used in your browser. Arbitrary DNS names, HTTPS URLs and paths are not accepted by the current URL setting.

## Sign-in and Remember me

Select **Remember me** to stay signed in until logout or password reset, with no server-side expiry. Remembered sessions survive container restart and recreation when the same appdata is retained. Without Remember me, sessions expire after 12 hours and are lost when the container restarts.

The cookie contains a random session token, never your password. Remembered sessions are stored in appdata as token digests. Up to 32 browser sessions are retained; additional sign-ins evict the oldest session.

Browsers limit cookie lifetimes. GPU Tuner sets a 400-day cookie and renews it during authenticated use. Clearing browser data, privacy settings, or leaving the app unused beyond the browser's cookie lifetime may require signing in again.

Use **Log out** to revoke the current browser session and return to the login page.

## Reset the WebUI password

On the login page, expand **Forgot password?** and select **Copy command**. Paste the command into **Terminal** in the Unraid WebGUI.

For a container named `gpu-tuner`, the command is:

```bash
docker exec -it gpu-tuner python /usr/local/bin/gpu-tuner-entrypoint --reset-password
```

Replace `gpu-tuner` with your container name if different. The login page uses the container name supplied by Unraid to generate its command.

1. Enter a new password of 12–1024 characters. Input is hidden.
2. Enter the same password again to confirm.
3. Wait for `WebUI password reset. Sign in with the new password; no restart needed.`
4. Open the WebUI and sign in with the new password.

The current password is not required; administrator access to the Unraid terminal authorizes recovery. No container restart or template edit is needed. Existing browser sessions are rejected on their next authenticated request. GPU settings and the separate desktop token are preserved. Invalid input or a confirmation mismatch leaves the current password unchanged.

### Recovery when the container cannot start

Stop the container and back up appdata. Remove only `ui/.config/gpu-tuner/password.hash` from its appdata directory, then add an **Initial WebUI Password** variable with key `GPU_TUNER_INITIAL_PASSWORD` in the container editor. Enter the new password and select **Apply**.

After signing in, remove that variable in Advanced View and Apply again. Preserve the remaining appdata files to retain GPU settings.

## Persistent data

Files under the host appdata directory:

| Path | Purpose |
| --- | --- |
| `config/config.json` | Daemon configuration and GPU budget |
| `state/state.json` | Saved GPU settings |
| `ui/.config/gpu-tuner/password.hash` | WebUI password hash |
| `ui/.config/gpu-tuner/browser-sessions.json` | Remembered browser token digests |
| `ui/.config/gpu-tuner/token` | Separate desktop authentication token |

Keep the same appdata mapping when restarting, recreating or upgrading the container. Removing a container does not remove its bind-mounted appdata. Do not share the directory between running instances or publish credential files in support requests.

## Updates and backups

Before updating, stop the container and back up its appdata. Update the image through Unraid while keeping the same appdata path, GPU parameters, WebUI port and URL.

After updating, check GPU telemetry and saved settings. Remembered sessions should remain valid; ordinary sessions require signing in again. For rollback, stop the container, select the previous versioned image, and restore the matching appdata backup if needed.

The template's 45-second stop timeout allows supervised shutdown and automatic fan-control restoration.

## Troubleshooting and support

- **HTTP 421 or 403:** ensure WebUI URL matches the browser address, including the port. Use an HTTP URL with a literal IPv4 address.
- **Container exits during first start:** check the logs and ensure the initial password contains 12–1024 characters.
- **Password authentication unavailable:** check the logs and use password recovery if the stored hash is missing or invalid.
- **GPU not detected:** check the NVIDIA Driver plugin, `nvidia-smi`, and the template's `--gpus all` parameter. Ensure the GPU is available to Docker.
- **Controls unavailable:** some GPU models expose monitoring only. Check the capabilities reported in the WebUI.
- **Icon missing:** the template uses the public [PNG icon](https://raw.githubusercontent.com/0sy/gpu-tuner-unraid/main/icon.png). Refresh the Unraid Docker page and check that the server can download that URL.

Report Unraid packaging issues at [0sy/gpu-tuner-unraid/issues](https://github.com/0sy/gpu-tuner-unraid/issues). Include the Unraid version, NVIDIA driver version, GPU model, image tag and relevant logs. Remove passwords, session tokens and credential hashes before sharing files or logs.
