<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Copyright (C) Opinsys Oy 2026 -->

# Puu OS bootc container

Puu OS is a bootc container operating system. It provides the means to create
your own images and the supply chain for OS updates using any standard OCI
container repository.

**NOTE:** This is a pre-release project i.e., backwards compatibility breaking
updates still happen commonly.

## Development

    make help

## Components

Puu OS builds on top of the following open source projects:

- [bootc](https://bootc.dev/)
- [Buildroot](https://buildroot.net)
- [composefs](https://www.cncf.io/projects/composefs/)
- [GNOME](https://www.gnome.org)
- [GStreamer](https://gstreamer.freedesktop.org)
- [K3s](https://k3s.io/)
- [Linux](https://kernel.org)
- [LiteLLM](https://www.litellm.ai)
- [PipeWire](https://pipewire.org)
- [Skopeo](https://skopeo.org)
- [systemd](https://systemd.io)
- [Unified Kernel Image
  (UKI)](https://uapi-group.org/specifications/specs/unified_kernel_image/)

## Services

| Service    | URL                     |
| ---------- | ------------------------|
| Oksa       | http://oksa.puu.local   |
| OpenAI API | http://api.puu.local/v1 |
| Open WebUI | http://puu.local/       |
| Kubernetes | https://puu.local:6443  |

Services URLs are still very much due to constant change.

## LiteLLM

Store a master key in order to require credentials for every inference request:

```sh
sudo systemd-creds encrypt --with-key=host - /etc/credstore.encrypted/litellm-master-key
sudo systemctl restart puu-k3s-addons.service
```

## License

Puu OS bootc container is licensed under the GNU General Public License 2.0 or a
later version. Refer to [`LICENSE`](LICENSE) for details.

Artwork in [`br2-external/board/puu/splash.png`](br2-external/board/puu/splash.png) is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).

The Puu OS logo uses [IBM Plex Mono](https://github.com/IBM/plex), which
is licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org/open-font-license-official-text/).
