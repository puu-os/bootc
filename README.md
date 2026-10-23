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
- [llm-d](https://llm-d.ai/)
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
shows service startup, the Bonsai model download and capacity calibration.
Network settings are available, and TTY/SSH can be used for recovery. When
setup completes, Continue returns to the login screen and the next login
opens GNOME.

Systems without a suitable NVIDIA GPU complete setup with local inference
disabled.

Calibration targets up to 8,192 context tokens and tests 1, 2, 4 and 8
simultaneous requests within the measured KV cache budget. Each profile
runs one warmup and three full-context rounds generating 32 tokens per
request. Higher concurrency is selected only when its median aggregate
output throughput improves by at least 10% over the single-request
baseline at the same context length. This favors multi-user throughput
over maximum context length; the score includes prompt processing.

To calibrate again:

```sh
sudo /usr/libexec/puu/oksa-calibrate --force
```

## Development

### Listing the available boards

    make list

### Building

    make BOARD=puu_amd64 build
    make BOARD=puu_arm64 build
    make BOARD=puu_amd64 burn DEVICE=/dev/sdX
    make lint

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
