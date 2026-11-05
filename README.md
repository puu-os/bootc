<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Copyright (C) Opinsys Oy 2026 -->

# Puu OS bootc container

Puu OS is a bootc container operating system. It provides the means to create
your own images and the supply chain for OS updates using any standard OCI
container repository.

Puu OS builds on top of the following open source projects:

- [bootc](https://bootc.dev/)
- [Buildroot](https://buildroot.net)
- [composefs](https://www.cncf.io/projects/composefs/)
- [GNOME](https://www.gnome.org)
- [GStreamer](https://gstreamer.freedesktop.org)
- [K3s](https://k3s.io/)
- [Linux](https://kernel.org)
- [LiteLLM](https://www.litellm.ai)
- [OSTree](https://github.com/ostreedev)
- [PipeWire](https://pipewire.org)
- [Skopeo](https://skopeo.org)
- [systemd](https://systemd.io)
- [Unified Kernel Image
  (UKI)](https://uapi-group.org/specifications/specs/unified_kernel_image/)

## Services

Default network services available after installation:

- Open WebUI: `http://puu.local/`
- OpenAI-compatible API: `http://api.puu.local/v1`
- Kubernetes (K3s): `https://puu.local:6443`
- SSH: port 22

Puu publishes `api.puu.local` over mDNS. Linux clients using `mdns4_minimal`
need `mdns4` before `resolve`/`dns` in `/etc/nsswitch.conf` and `.local`
allowed in `/etc/mdns.allow`, or a local DNS record for the API hostname.

LiteLLM inference is keyless by default; supplied per-user keys are still
validated. Anonymous requests remain unrestricted. Store a master key to
require credentials for every inference request:

```sh
sudo systemd-creds encrypt --with-key=host - /etc/credstore.encrypted/litellm-master-key
sudo systemctl restart puu-k3s-addons.service
```

## First boot

After the first login, a new GNOME installation opens a setup session that
shows service startup, configured model downloads and per-model capacity
calibration. Installed models are discovered and deployed through one serving
planner. Network settings and TTY/SSH are available for recovery. When setup
completes, Continue returns to the login screen and the next login opens GNOME.

To recalibrate an installed model:

```sh
sudo /usr/libexec/puu/oksa-calibrate --model MODEL --force
sudo /usr/libexec/puu/oksa-plan-vllm-workloads --require-reconcile
```

## Development

### Listing the available boards

    make list

### Building

    make BOARD=puu_amd64 refresh-flatpaks
    make BOARD=puu_amd64 build PUU_COSIGN_KEYS=
    make BOARD=puu_arm64 refresh-flatpaks
    make BOARD=puu_arm64 build PUU_COSIGN_KEYS=
    make BOARD=puu_amd64 burn DEVICE=/dev/sdX
    make lint

Set `PUU_COSIGN_KEYS` to a colon-separated list of signing keys for signed builds;
set it empty explicitly for unsigned builds.

`build` reapplies the board configuration only when its inputs change. Once a
build succeeds, repeating it with unchanged sources, options, package state and
outputs skips Buildroot and image generation. Source content, modes, symlink
names and deletions are tracked, including uncommitted and untracked files under
the source directories. Generated `dist` and Python cache directories are ignored.
Output identities include filesystem metadata (including ctime), avoiding a
complete reread of the installed trees on every build.

Use `make BOARD=puu_amd64 build PUU_COSIGN_KEYS= FORCE=1` to run Buildroot even
when the build is current, and `make BOARD=puu_amd64 configure` to force defconfig.
Manual package operations invalidate the successful-build record. Use `FORCE=1`
after editing extracted Buildroot sources directly; prefer editing this external
tree instead. Existing package stamps are adopted on the first tracked build.
Later package recipe/patch changes require `make clean` because Buildroot does
not automatically rebuild reverse dependencies. Changes under `package/oksa/files`
and `package/puu-firstboot/files` clean and rebuild those local packages automatically.

Flatpak preseeding uses an immutable, per-architecture snapshot. Only
`refresh-flatpaks` contacts the remote and resolves new app/runtime commits;
ordinary builds validate and copy the selected snapshot. Refresh before the
first build and after changing `overlay/usr/share/puu/flatpaks.list` or its remote
configuration. To omit preseeding, set `PUU_FLATPAK_PRESEED=0`.

Image assembly preserves Buildroot's base `rootfs.squashfs` and OCI archive.
The installer-specific filesystem is `rootfs-live.squashfs`. Live-rootfs,
signing and ISO stages keep independent successful-output records.

The primary installer artifact is `build/<board>/images/<board>.iso`. The same
hybrid ISO can be burned to optical media, attached as a virtual CD/DVD (including
PiKVM), or written directly to a USB stick with `make burn`. UEFI boot is required.
Do not extract the ISO or modify its partition table after writing it.

### Releasing

    make release VERSION=N
    make publish

## License

Puu OS bootc container is licensed under the GNU General Public License 2.0 or a
later version. Refer to [`LICENSE`](LICENSE) for details.

Artwork in [`artwork/`](artwork/) is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).

The Puu OS logo uses [IBM Plex Mono](https://github.com/IBM/plex), which
is licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org/open-font-license-official-text/).
