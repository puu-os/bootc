<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Copyright (C) Opinsys Oy 2026 -->

# Puu OS bootc container

Puu OS is a bootc container operating system. It provides the means to
create your own images and the supply chain for OS updates using any
standard OCI container repository.

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


## Images & Variants

Puu OS publishes bootc container images as multi-arch OCI manifests:

`quay.io/puu-os/<variant>:<version>`

- **`gnome`**: Default desktop variant featuring GNOME, K3s, and AI services.

### System Management

```sh
# Inspect the active deployment
bootc status

# Upgrade the current variant to the latest image
bootc upgrade

# Switch to a different variant or version
bootc switch quay.io/puu-os/gnome:1
```

## Building

Builds require a board. List them with `make list` (`puu_amd64`,
`puu_arm64`).

```sh
make BOARD=puu_amd64 build
make BOARD=puu_amd64 burn DEVICE=/dev/sdX
make lint
```

## Releasing

```sh
make release VERSION=N
make publish
```

## License

Puu OS bootc container is licensed under the GNU General Public License 2.0 or a
later version. Refer to [`LICENSE`](LICENSE) for details.

Artwork in [`artwork/`](artwork/) is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).

The Puu OS logo uses [IBM Plex Mono](https://github.com/IBM/plex), which
is licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org/open-font-license-official-text/).

### Third-party software

GPL-2.0-or-later covers only Puu OS's own code and build glue. The shipped
image bundles independent upstream projects, each under its own license as
recorded in that package's Buildroot `.mk` (`*_LICENSE` and
`*_LICENSE_FILES`).
