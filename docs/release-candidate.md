# Unraid beta release

First beta image tag: `1.3.1-unraid.1-beta.1`, based on upstream GPU Tuner 1.3.1.

The fork adds Unraid Docker packaging, persistent appdata, a password login page, independent browser sessions, remembered logins across container recreation, password-reset command and recovery guidance, light/dark mode controls and a Community Applications template. It preserves upstream MIT attribution.

## Prepared artifacts

- `container/Dockerfile`: NVIDIA-enabled container image with root daemon and unprivileged WebUI.
- `templates/gpu-tuner.xml`: Community Applications template pointing to the intended public GHCR image.
- `ca_profile.xml`: repository metadata.
- `icon.svg`: vector source; `icon.png`: transparent 256-pixel Unraid icon. Publish both; templates use the PNG.
- `.github/workflows/tests.yml`: container-based Unraid, installed-image and offline upstream checks.
- `.github/workflows/publish.yml`: manually dispatched build/test/push pipeline. No automatic publishing on commit or tag creation.
- `docs/unraid.md`: installation, credential recovery, remembered-login behavior and old-layout migration.

## Publication status

Published beta `1.3.1-unraid.1-beta.1` and `latest` from commit `a9ba5116ddc49ee22e3d7dd9ebc7468684e4abbe`. CI and publication succeeded. Both tags were pulled anonymously and resolve to `sha256:d636c8ed68ca7316cdcd7cb80b3ca9b9e3582685767779067579bc84b81a128a`. The downloaded image passed first-run/login/reset checks, and public PNG/template/profile verification passed.

## Remaining distribution steps

1. Push the documentation update and local Git release tag.
2. Submit `https://github.com/0sy/gpu-tuner-unraid` through the Community Applications portal.
3. Run portal Validate and Scan, resolve any findings, and submit the beta listing for review.

The local template is installed at `/boot/config/plugins/dockerMan/templates-user/my-gpu-tuner-ca-test.xml`. Its mandatory initial-password field is intentionally empty. Set it in the Unraid form before Apply. After successful login, remove the variable entirely in Advanced View and Apply again; an empty required field cannot be saved. The preview `gpu-tuner-install-test` and older development controller remain stopped; the user-installed `gpu-tuner-ca-test` is the active controller.

## Local release checks

The old-layout migration copy passed a live GPU-daemon startup while preserving original data. The 130 offline daemon/fleet/demo tests passed after aligning fan-mode fixtures with the existing Unraid default. The installed-image first-run/reset smoke check passed, including private root-owned appdata parents. All 62 Unraid regression tests passed, covering authentication, renamed-container recovery commands and password input validation (192 automated tests total). See `docs/unraid.md` for exact results and remaining manual checks.
