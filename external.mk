# OKSA snapshots the enabled Python runtime when its prerequisites are expanded.
PUU_PYTHON_PACKAGE_MKS := $(sort $(wildcard $(BR2_EXTERNAL_PUU_PATH)/package/python-*/*.mk))
include $(PUU_PYTHON_PACKAGE_MKS)
include $(sort $(filter-out $(PUU_PYTHON_PACKAGE_MKS),$(wildcard $(BR2_EXTERNAL_PUU_PATH)/package/*/*.mk)))
