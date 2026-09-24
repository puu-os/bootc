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
CACHE_DIR             := $(HOME)/.cache/puu-os
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
BOARDS                := $(patsubst configs/%_defconfig,%,$(wildcard configs/*_defconfig))

ifdef BOARD
ifeq ($(wildcard configs/$(BOARD)_defconfig),)
$(error unknown BOARD=$(BOARD); see `make list`)
endif
SDK_ARCH := $(shell awk -F= '/^BR2_(x86_64|aarch64)=y$$/ { sub(/^BR2_/, "", $$1); print $$1; exit }' \
	configs/$(BOARD)_defconfig)
ifeq ($(SDK_ARCH),)
$(error configs/$(BOARD)_defconfig must select BR2_x86_64 or BR2_aarch64)
endif
else
SDK_ARCH :=
endif

SDK_INSTALL_DIR       := $(SDK_INSTALL_ROOT)/$(SDK_ARCH)/$(SDK_ARCH)-puu-linux-gnu_sdk-buildroot
SDK_INSTALL_STAMP     := $(SDK_INSTALL_ROOT)/.stamp/$(SDK_ARCH)/install

ifneq ($(filter configure build burn,$(MAKECMDGOALS)),)
ifndef BOARD
$(error BOARD is required, e.g.: make build BOARD=puu_amd64)
endif
endif

ifneq ($(filter build,$(MAKECMDGOALS)),)
ifeq ($(origin PUU_COSIGN_KEYS),undefined)
$(error PUU_COSIGN_KEYS is required; set it empty for an unsigned build)
endif
endif

define buildroot
  $(MAKE) -j$(BUILDROOT_JOBS) -l$(BUILDROOT_JOBS) -C "$(BUILDROOT_SRC_DIR)" O="$(BOARD_DIR)" \
    BR2_EXTERNAL="$(CURDIR)" \
    BR2_CCACHE_DIR="$(CCACHE_DIR)" BR2_DL_DIR="$(BUILDROOT_DL_DIR)" \
    SDK_INSTALL_DIR="$(SDK_INSTALL_DIR)" \
    PUU_VERSION="$(PUU_VERSION)" \
    PUU_VARIANT="$(PUU_VARIANT)" \
    SOURCE_DATE_EPOCH="$(SOURCE_DATE_EPOCH)" \
    $(1)
endef

.PHONY: default help list download sdk clean-sdk configure build burn clean lint release publish

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
	./scripts/build-sdk.sh "$*" "sdk/$*_defconfig" "$(BUILDROOT_TARBALL)" "$(BUILDROOT_HASH)" \
		"$(SDK_BUILD_DIR)" "$(SDK_INSTALL_ROOT)" "$(SDK_LOCK)" "$(BUILDROOT_DL_DIR)" "$(SOURCE_DATE_EPOCH)"

$(BUILDROOT_PATCH_STAMP): $(BUILDROOT_TARBALL) $(BUILDROOT_PATCH)
	./scripts/setup-buildroot.sh "$(BUILDROOT_PATCH_LOCK)" "$(BUILDROOT_TARBALL)" "$(BUILDROOT_HASH)" \
		"$(BOARD_DIR)" "$(BUILDROOT_SRC_DIR)" "$@" "$(BUILDROOT_PATCH)"

configure: $(SDK_INSTALL_STAMP) $(BUILDROOT_PATCH_STAMP) ## Run <BOARD>_defconfig
	$(call buildroot,$(BOARD)_defconfig)

build: configure ## Build <BOARD>
	$(call buildroot,BR2_CCACHE=y)

burn: ## Write <BOARD>.img to DEVICE
	./scripts/burn.sh "$(BOARD_DIR)/images/$(BOARD).img" "$(DEVICE)"

clean: ## Remove build artifacts
	rm -rf "$(abspath $(OUTPUT_DIR))"

clean-sdk: ## Remove cached SDK toolchains
	rm -rf "$(SDK_DIR)"

lint: ## Run linters
	./scripts/lint.sh

release: ## Tag a signed release (VERSION=N)
	./scripts/release.sh "$(VERSION)"

publish: ## Publish a GitLab release from a tag
	./scripts/publish.sh "$(VERSION)"
