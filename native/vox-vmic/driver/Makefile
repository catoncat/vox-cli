TARGET_NAME := VoxVirtualMic
BUILD_DIR := build
BUNDLE_DIR := $(BUILD_DIR)/$(TARGET_NAME).driver
CONTENTS_DIR := $(BUNDLE_DIR)/Contents
MACOS_DIR := $(CONTENTS_DIR)/MacOS
SOURCES := src/VirtualMicDriver.c ../shared/VMicBridge.c
HEADERS := src/VirtualMicDriver.h ../shared/include/VMicBridge.h
CFLAGS := -std=c11 -Wall -Wextra -fPIC -I../shared/include
LDFLAGS := -bundle -framework CoreAudio -framework CoreFoundation -framework CoreServices

.PHONY: all clean

all: $(MACOS_DIR)/$(TARGET_NAME) $(CONTENTS_DIR)/Info.plist

$(MACOS_DIR)/$(TARGET_NAME): $(SOURCES) $(HEADERS)
	mkdir -p $(MACOS_DIR)
	clang $(CFLAGS) $(SOURCES) -o $@ $(LDFLAGS)

$(CONTENTS_DIR)/Info.plist: Info.plist
	mkdir -p $(CONTENTS_DIR)
	cp Info.plist $@

clean:
	rm -rf $(BUILD_DIR)
