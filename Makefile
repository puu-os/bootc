# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

SHELL := bash
.ONESHELL:
.SHELLFLAGS := -euo pipefail -c

PUU_VERSION           ?= 1
PUU_VARIANT           ?= gnome
SOURCE_DATE_EPOCH     ?= 1735689600

OUTPUT_DIR            ?= build
BOARD_DIR             := $(abspath $(OUTPUT_DIR)/$(BOARD))
CACHE_DIR             := $(HOME)/.cache/puu
DOWNLOAD_DIR          := $(CACHE_DIR)/download
CCACHE_DIR            := $(CACHE_DIR)/ccache
STAMP_DIR             := $(BOARD_DIR)/stamp

BUILDROOT_JOBS        ?= 8
BUILDROOT_VERSION     ?= 2026.08
BUILDROOT_HASH        ?= 87aaca4164ea9d5c8085854953018263f7963f07c22e73a2a2185cc98c581c34
BUILDROOT_URL         ?= https://buildroot.org/downloads/buildroot-$(BUILDROOT_VERSION).tar.xz
BUILDROOT_DL_DIR      := $(DOWNLOAD_DIR)/buildroot
BUILDROOT_TARBALL     := $(BUILDROOT_DL_DIR)/source/$(BUILDROOT_HASH)/buildroot-$(BUILDROOT_VERSION).tar.xz
BUILDROOT_PATCH       := $(CURDIR)/buildroot.patch
BUILDROOT_PATCH_KEY   := $(shell sha256sum $(BUILDROOT_PATCH) | cut -d' ' -f1)
BUILDROOT_SRC_DIR     := $(BOARD_DIR)/src/$(BUILDROOT_HASH)/buildroot-$(BUILDROOT_VERSION)
BUILDROOT_PATCH_LOCK  := $(abspath $(OUTPUT_DIR))/.locks/$(BOARD).patch
BUILDROOT_PATCH_STAMP := $(STAMP_DIR)/$(BUILDROOT_VERSION)/$(BUILDROOT_HASH)/$(BUILDROOT_PATCH_KEY)/patch

SDK_DIR               := $(CACHE_DIR)/sdk
SDK_CACHE_DIR         := $(SDK_DIR)/$(BUILDROOT_VERSION)
SDK_CONFIG_HASH       := $(shell sha256sum $(sort $(wildcard sdk/*_defconfig)) | sha256sum | cut -d' ' -f1)
SDK_STAMP_KEY         := $(shell printf '%s\n' '$(notdir $(BUILDROOT_TARBALL))' '$(BUILDROOT_HASH)' '$(SOURCE_DATE_EPOCH)' '$(SDK_CONFIG_HASH)' | sha256sum | cut -d' ' -f1)
SDK_BUILD_ROOT        := $(abspath $(OUTPUT_DIR))/sdk/$(BUILDROOT_VERSION)
SDK_BUILD_DIR         := $(SDK_BUILD_ROOT)/$(SDK_STAMP_KEY)
SDK_INSTALL_ROOT      := $(SDK_CACHE_DIR)/$(SDK_STAMP_KEY)
SDK_LOCK              := $(SDK_CACHE_DIR)/.locks/$(SDK_STAMP_KEY)
SDK_ARCHS             := $(patsubst sdk/%_defconfig,%,$(wildcard sdk/*_defconfig))
BOARDS                := $(patsubst br2-external/configs/%_defconfig,%,$(wildcard br2-external/configs/*_defconfig))

ifdef BOARD
ifeq ($(wildcard br2-external/configs/$(BOARD)_defconfig),)
$(error unknown BOARD=$(BOARD); see `make list`)
endif
SDK_ARCH := $(shell awk -F= '/^BR2_(x86_64|aarch64)=y$$/ { sub(/^BR2_/, "", $$1); print $$1; exit }' \
	br2-external/configs/$(BOARD)_defconfig)
ifeq ($(SDK_ARCH),)
$(error br2-external/configs/$(BOARD)_defconfig must select BR2_x86_64 or BR2_aarch64)
endif
PUU_ARCH := $(if $(filter x86_64,$(SDK_ARCH)),amd64,arm64)
else
SDK_ARCH :=
PUU_ARCH :=
endif

SDK_INSTALL_DIR       := $(SDK_INSTALL_ROOT)/$(SDK_ARCH)/$(SDK_ARCH)-puu-linux-gnu_sdk-buildroot
SDK_INSTALL_STAMP     := $(SDK_INSTALL_ROOT)/.stamp/$(SDK_ARCH)/install

ifneq ($(filter configure build flatpak-update burn,$(MAKECMDGOALS)),)
ifndef BOARD
$(error BOARD is required, e.g.: make build BOARD=puu_amd64)
endif
endif

ifneq ($(filter build,$(MAKECMDGOALS)),)
ifeq ($(origin PUU_COSIGN_KEYS),undefined)
$(error PUU_COSIGN_KEYS is required; set it empty for an unsigned build)
endif
endif

export BOARD BOARD_DIR BUILDROOT_SRC_DIR BUILDROOT_JOBS BUILDROOT_DL_DIR
export BUILDROOT_PATCH_LOCK BUILDROOT_TARBALL BUILDROOT_HASH BUILDROOT_PATCH_STAMP BUILDROOT_PATCH
export SDK_INSTALL_DIR SDK_INSTALL_STAMP CCACHE_DIR PUU_VERSION PUU_VARIANT SOURCE_DATE_EPOCH

.PHONY: default help list download sdk clean-sdk configure build flatpak-update burn clean lint release publish

default: help

help: ## Show this help
	@awk -F ':[^#]*## ?' '/^[a-z_-]+:[^#]*##/{printf "  make %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

list: ## List available defconfigs
	@printf '%s\n' $(BOARDS)

download: $(BUILDROOT_TARBALL) ## Prime the download cache

$(BUILDROOT_TARBALL):
	./scripts/download.sh "$(BUILDROOT_URL)" "$(BUILDROOT_HASH)" "$@"

.PRECIOUS: $(SDK_INSTALL_ROOT)/.stamp/%/install

sdk-%: $(SDK_INSTALL_ROOT)/.stamp/%/install ;

sdk: $(addprefix sdk-,$(SDK_ARCHS)) ## Build and cache SDK toolchains
	rm -rf "$(SDK_BUILD_DIR)"

$(SDK_INSTALL_ROOT)/.stamp/%/install: $(BUILDROOT_TARBALL) sdk/%_defconfig
	./scripts/buildroot-sdk.sh "$*" "sdk/$*_defconfig" "$(BUILDROOT_TARBALL)" "$(BUILDROOT_HASH)" \
		"$(SDK_BUILD_DIR)" "$(SDK_INSTALL_ROOT)" "$(SDK_LOCK)" "$(BUILDROOT_DL_DIR)" "$(SOURCE_DATE_EPOCH)"

configure: $(BUILDROOT_TARBALL) $(SDK_INSTALL_STAMP) ## Force <BOARD> configuration
	./scripts/buildroot-board.sh configure

build: $(BUILDROOT_TARBALL) $(SDK_INSTALL_STAMP) ## Incrementally build <BOARD> (FORCE=1 to run again)
	./scripts/buildroot-board.sh build

flatpak-update: ## Update the pinned Flatpak snapshot for <BOARD>
	PUU_ARCH="$(PUU_ARCH)" BR2_EXTERNAL_PUU_PATH="$(CURDIR)/br2-external" ./scripts/flatpak-update.sh

burn: ## Write <BOARD>.iso to DEVICE
	./scripts/burn.sh "$(BOARD_DIR)/images/$(BOARD).iso" "$(DEVICE)"

clean: ## Remove build artifacts
	rm -rf "$(abspath $(OUTPUT_DIR))"

clean-sdk: ## Remove cached SDK toolchains
	rm -rf "$(SDK_DIR)"

lint: ## Run linters
	./scripts/lint.sh

release: ## Tag a signed release (VERSION=N)
	./scripts/release-tag.sh "$(VERSION)"

publish: ## Publish a GitLab release from a tag
	./scripts/release-publish.sh "$(VERSION)"
