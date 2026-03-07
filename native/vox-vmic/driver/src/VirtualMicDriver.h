#ifndef VIRTUAL_MIC_DRIVER_H
#define VIRTUAL_MIC_DRIVER_H

#include <CoreAudio/AudioServerPlugIn.h>

enum {
    kObjectID_Device = 2,
    kObjectID_Stream_Input = 3,
};

enum {
    kVirtualMicChannelCount = 1,
};

extern void *VoxVMicDriverFactory(CFAllocatorRef allocator, CFUUIDRef typeUUID);

#endif
