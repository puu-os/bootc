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
- [KubeAI](https://www.kubeai.org)
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
| LiteLLM UI | http://api.puu.local/ui |
| OpenAI API | http://api.puu.local/v1 |
| Open WebUI | http://puu.local/       |
| Kubernetes | https://puu.local:6443  |

Services URLs are still very much due to constant change.

## LiteLLM

Store a master key in order to require credentials for every inference request and sign in to the LiteLLM UI (`admin`):

```sh
sudo systemd-creds encrypt --with-key=host - /etc/credstore.encrypted/litellm-master-key
sudo systemctl restart puu-k3s-addons.service
```

## KubeAI models

No model is enabled by default. On a supported NVIDIA system, opt in to
Ternary Bonsai by applying the bundled manifest for the node's architecture:

```sh
# Choose amd64 or arm64 to match the NVIDIA node.
arch=amd64
sudo kubectl --kubeconfig=/var/lib/rancher/k3s/k3s.yaml apply -f \
  "/usr/share/puu/vllm/models/$arch/ternary-bonsai-2-27b.yaml"
```

In the LiteLLM Admin UI, add a model with public name
`ternary-bonsai-2-27b`, provider model `openai/ternary-bonsai-2-27b`, and API
base `http://kubeai.oksa.svc.cluster.local/openai/v1`. KubeAI does not require
an upstream key; use a placeholder such as `unused` in LiteLLM's API key
field.

The model starts on the first inference request and scales back to zero
after five idle minutes. The first request can take longer while the image
and model weights download. To remove the model, delete its Kubernetes
`Model` in the `oksa` namespace and remove its LiteLLM mapping.

## License

Puu OS bootc container is licensed under the GNU General Public License 2.0 or a
later version. Refer to [`LICENSE`](LICENSE) for details.

Artwork in [`br2-external/board/puu/splash.png`](br2-external/board/puu/splash.png) is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).

The Puu OS logo uses [IBM Plex Mono](https://github.com/IBM/plex), which
is licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org/open-font-license-official-text/).
