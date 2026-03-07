#include "VirtualMicDriver.h"

#include "VMicBridge.h"

#include <CoreAudio/AudioHardware.h>
#include <CoreAudio/AudioHardwareBase.h>
#include <CoreFoundation/CFString.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <mach/mach_time.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static HRESULT STDMETHODCALLTYPE QueryInterface(void *inDriver, REFIID inUUID, LPVOID *outInterface);
static ULONG STDMETHODCALLTYPE AddRef(void *inDriver);
static ULONG STDMETHODCALLTYPE Release(void *inDriver);
static OSStatus STDMETHODCALLTYPE Initialize(AudioServerPlugInDriverRef inDriver, AudioServerPlugInHostRef inHost);
static OSStatus STDMETHODCALLTYPE CreateDevice(AudioServerPlugInDriverRef inDriver, CFDictionaryRef inDescription, const AudioServerPlugInClientInfo *inClientInfo, AudioObjectID *outDeviceObjectID);
static OSStatus STDMETHODCALLTYPE DestroyDevice(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID);
static OSStatus STDMETHODCALLTYPE AddDeviceClient(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, const AudioServerPlugInClientInfo *inClientInfo);
static OSStatus STDMETHODCALLTYPE RemoveDeviceClient(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, const AudioServerPlugInClientInfo *inClientInfo);
static OSStatus STDMETHODCALLTYPE PerformDeviceConfigurationChange(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt64 inChangeAction, void *inChangeInfo);
static OSStatus STDMETHODCALLTYPE AbortDeviceConfigurationChange(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt64 inChangeAction, void *inChangeInfo);
static Boolean STDMETHODCALLTYPE HasProperty(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress);
static OSStatus STDMETHODCALLTYPE IsPropertySettable(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, Boolean *outIsSettable);
static OSStatus STDMETHODCALLTYPE GetPropertyDataSize(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 *outDataSize);
static OSStatus STDMETHODCALLTYPE GetPropertyData(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 inDataSize, UInt32 *outDataSize, void *outData);
static OSStatus STDMETHODCALLTYPE SetPropertyData(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 inDataSize, const void *inData);
static OSStatus STDMETHODCALLTYPE StartIO(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID);
static OSStatus STDMETHODCALLTYPE StopIO(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID);
static OSStatus STDMETHODCALLTYPE GetZeroTimeStamp(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, Float64 *outSampleTime, UInt64 *outHostTime, UInt64 *outSeed);
static OSStatus STDMETHODCALLTYPE WillDoIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, Boolean *outWillDo, Boolean *outWillDoInPlace);
static OSStatus STDMETHODCALLTYPE BeginIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo);
static OSStatus STDMETHODCALLTYPE DoIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, AudioObjectID inStreamObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo, void *ioMainBuffer, void *ioSecondaryBuffer);
static OSStatus STDMETHODCALLTYPE EndIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo);

static AudioServerPlugInDriverInterface gDriverInterface = {
    NULL,
    QueryInterface,
    AddRef,
    Release,
    Initialize,
    CreateDevice,
    DestroyDevice,
    AddDeviceClient,
    RemoveDeviceClient,
    PerformDeviceConfigurationChange,
    AbortDeviceConfigurationChange,
    HasProperty,
    IsPropertySettable,
    GetPropertyDataSize,
    GetPropertyData,
    SetPropertyData,
    StartIO,
    StopIO,
    GetZeroTimeStamp,
    WillDoIOOperation,
    BeginIOOperation,
    DoIOOperation,
    EndIOOperation,
};

static AudioServerPlugInDriverInterface *gDriverInterfacePtr = &gDriverInterface;
static CFUUIDRef gFactoryUUID = NULL;
static AudioServerPlugInDriverRef gDriverRef = &gDriverInterfacePtr;
static const CFUUIDBytes kFactoryUUID = {0xC1, 0x8C, 0xE7, 0xC2, 0x98, 0x9E, 0x4E, 0x60, 0x92, 0x20, 0x59, 0xE9, 0x66, 0xA9, 0x26, 0x9A};
static UInt32 gRefCount = 1;
static AudioServerPlugInHostRef gHost = NULL;
static UInt32 gActiveIOCount = 0;
static UInt64 gClockSeed = 1;
static UInt64 gStartHostTime = 0;
static UInt64 gAnchorHostTime = 0;
static Float64 gHostTicksPerFrame = 0;
static UInt64 gPeriodCounter = 0;
static const UInt32 kZeroTimeStampPeriodFrames = 192;
static int gUDPSocket = -1;
static float *gRingBuffer = NULL;
static UInt32 gRingCapacityFrames = 48000 * 30;
static UInt32 gRingReadFrame = 0;
static UInt32 gRingWriteFrame = 0;
static UInt32 gRingQueuedFrames = 0;
static const UInt16 kUDPPort = 47211;

static bool UUIDEqual(REFIID left, CFUUIDRef right) {
    CFUUIDBytes bytes = CFUUIDGetUUIDBytes(right);
    return memcmp(&left, &bytes, sizeof(CFUUIDBytes)) == 0;
}

static void ResetRing(void) {
    gRingReadFrame = 0;
    gRingWriteFrame = 0;
    gRingQueuedFrames = 0;
}

static OSStatus EnsureSocketReady(void) {
    if (gUDPSocket >= 0) {
        return 0;
    }
    gUDPSocket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (gUDPSocket < 0) {
        return kAudioHardwareUnspecifiedError;
    }
    int flags = fcntl(gUDPSocket, F_GETFL, 0);
    if (flags >= 0) {
        (void)fcntl(gUDPSocket, F_SETFL, flags | O_NONBLOCK);
    }
    int reuse = 1;
    (void)setsockopt(gUDPSocket, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(kUDPPort);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(gUDPSocket, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(gUDPSocket);
        gUDPSocket = -1;
        return kAudioHardwareUnspecifiedError;
    }
    if (gRingBuffer == NULL) {
        gRingBuffer = (float *)calloc(gRingCapacityFrames, sizeof(float));
        if (gRingBuffer == NULL) {
            close(gUDPSocket);
            gUDPSocket = -1;
            return kAudioHardwareUnspecifiedError;
        }
    }
    ResetRing();
    return 0;
}

static void CloseSocket(void) {
    if (gUDPSocket >= 0) {
        close(gUDPSocket);
        gUDPSocket = -1;
    }
}

static void PumpUDP(void) {
    if (gUDPSocket < 0 || gRingBuffer == NULL) {
        return;
    }
    float packet[2048];
    while (1) {
        ssize_t nread = recv(gUDPSocket, packet, sizeof(packet), 0);
        if (nread <= 0) {
            break;
        }
        UInt32 frames = (UInt32)(nread / sizeof(float));
        for (UInt32 i = 0; i < frames; ++i) {
            if (gRingQueuedFrames >= gRingCapacityFrames) {
                gRingReadFrame = (gRingReadFrame + 1) % gRingCapacityFrames;
                gRingQueuedFrames -= 1;
            }
            gRingBuffer[gRingWriteFrame] = packet[i];
            gRingWriteFrame = (gRingWriteFrame + 1) % gRingCapacityFrames;
            gRingQueuedFrames += 1;
        }
    }
}

static void ReadFrames(Float32 *output, UInt32 requestedFrames) {
    UInt32 produced = 0;
    while (produced < requestedFrames && gRingQueuedFrames > 0) {
        output[produced++] = gRingBuffer[gRingReadFrame];
        gRingReadFrame = (gRingReadFrame + 1) % gRingCapacityFrames;
        gRingQueuedFrames -= 1;
    }
    while (produced < requestedFrames) {
        output[produced++] = 0.0f;
    }
}

static void UpdateClockCalibration(void) {
    struct mach_timebase_info timeBase;
    mach_timebase_info(&timeBase);
    long double hostClockFrequency = ((long double)timeBase.denom / (long double)timeBase.numer) * 1000000000.0L;
    gHostTicksPerFrame = (Float64)(hostClockFrequency / VMIC_DEFAULT_SAMPLE_RATE);
    if (gHostTicksPerFrame <= 0) {
        gHostTicksPerFrame = 1.0;
    }
}

static AudioStreamBasicDescription VirtualMicFormat(void) {
    AudioStreamBasicDescription asbd;
    memset(&asbd, 0, sizeof(asbd));
    asbd.mSampleRate = VMIC_DEFAULT_SAMPLE_RATE;
    asbd.mFormatID = kAudioFormatLinearPCM;
    asbd.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked;
    asbd.mBytesPerPacket = sizeof(Float32);
    asbd.mFramesPerPacket = 1;
    asbd.mBytesPerFrame = sizeof(Float32);
    asbd.mChannelsPerFrame = kVirtualMicChannelCount;
    asbd.mBitsPerChannel = 32;
    return asbd;
}

static AudioStreamRangedDescription VirtualMicRangedFormat(void) {
    AudioStreamRangedDescription ranged;
    memset(&ranged, 0, sizeof(ranged));
    ranged.mFormat = VirtualMicFormat();
    ranged.mSampleRateRange.mMinimum = VMIC_DEFAULT_SAMPLE_RATE;
    ranged.mSampleRateRange.mMaximum = VMIC_DEFAULT_SAMPLE_RATE;
    return ranged;
}

static OSStatus WriteCFString(CFStringRef value, UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    if (inDataSize < sizeof(CFStringRef)) {
        return kAudioHardwareBadPropertySizeError;
    }
    *((CFStringRef *)outData) = CFRetain(value);
    *outDataSize = sizeof(CFStringRef);
    return 0;
}

static OSStatus WriteObjectID(AudioObjectID value, UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    if (inDataSize < sizeof(AudioObjectID)) {
        return kAudioHardwareBadPropertySizeError;
    }
    *((AudioObjectID *)outData) = value;
    *outDataSize = sizeof(AudioObjectID);
    return 0;
}

static CFStringRef DeviceUID(void) {
    return CFSTR("com.envvar.vox.vmic.device");
}

static CFStringRef ManufacturerName(void) {
    return CFSTR("envvar");
}

static CFStringRef DeviceName(void) {
    return CFSTR("Vox Virtual Mic");
}

static CFStringRef StreamName(void) {
    return CFSTR("Vox Virtual Mic Input");
}

static CFStringRef ResourceBundlePath(void) {
    return CFSTR("/Library/Audio/Plug-Ins/HAL/VoxVirtualMic.driver");
}

void *VoxVMicDriverFactory(CFAllocatorRef allocator, CFUUIDRef typeUUID) {
    (void)allocator;
    if (!CFEqual(typeUUID, kAudioServerPlugInTypeUUID)) {
        return NULL;
    }
    if (gFactoryUUID == NULL) {
        gFactoryUUID = CFUUIDCreateFromUUIDBytes(kCFAllocatorDefault, kFactoryUUID);
    }
    CFPlugInAddInstanceForFactory(gFactoryUUID);
    return gDriverRef;
}

static HRESULT STDMETHODCALLTYPE QueryInterface(void *inDriver, REFIID inUUID, LPVOID *outInterface) {
    if (outInterface == NULL) {
        return E_POINTER;
    }

    if (UUIDEqual(inUUID, IUnknownUUID) || UUIDEqual(inUUID, kAudioServerPlugInDriverInterfaceUUID)) {
        *outInterface = gDriverRef;
        AddRef(gDriverRef);
        return S_OK;
    }

    *outInterface = NULL;
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE AddRef(void *inDriver) {
    (void)inDriver;
    gRefCount += 1;
    return gRefCount;
}

static ULONG STDMETHODCALLTYPE Release(void *inDriver) {
    (void)inDriver;
    if (gRefCount > 0) {
        gRefCount -= 1;
    }
    if (gRefCount == 0 && gFactoryUUID != NULL) {
        CFPlugInRemoveInstanceForFactory(gFactoryUUID);
    }
    return gRefCount;
}

static OSStatus STDMETHODCALLTYPE Initialize(AudioServerPlugInDriverRef inDriver, AudioServerPlugInHostRef inHost) {
    (void)inDriver;
    gHost = inHost;
    gClockSeed = 1;
    gStartHostTime = mach_absolute_time();
    return 0;
}

static OSStatus STDMETHODCALLTYPE CreateDevice(AudioServerPlugInDriverRef inDriver, CFDictionaryRef inDescription, const AudioServerPlugInClientInfo *inClientInfo, AudioObjectID *outDeviceObjectID) {
    (void)inDriver;
    (void)inDescription;
    (void)inClientInfo;
    (void)outDeviceObjectID;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus STDMETHODCALLTYPE DestroyDevice(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID) {
    (void)inDriver;
    (void)inDeviceObjectID;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus STDMETHODCALLTYPE AddDeviceClient(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, const AudioServerPlugInClientInfo *inClientInfo) {
    (void)inDriver;
    (void)inClientInfo;
    return inDeviceObjectID == kObjectID_Device ? 0 : kAudioHardwareBadObjectError;
}

static OSStatus STDMETHODCALLTYPE RemoveDeviceClient(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, const AudioServerPlugInClientInfo *inClientInfo) {
    (void)inDriver;
    (void)inClientInfo;
    return inDeviceObjectID == kObjectID_Device ? 0 : kAudioHardwareBadObjectError;
}

static OSStatus STDMETHODCALLTYPE PerformDeviceConfigurationChange(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt64 inChangeAction, void *inChangeInfo) {
    (void)inDriver;
    (void)inDeviceObjectID;
    (void)inChangeAction;
    (void)inChangeInfo;
    return 0;
}

static OSStatus STDMETHODCALLTYPE AbortDeviceConfigurationChange(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt64 inChangeAction, void *inChangeInfo) {
    (void)inDriver;
    (void)inDeviceObjectID;
    (void)inChangeAction;
    (void)inChangeInfo;
    return 0;
}

static Boolean STDMETHODCALLTYPE HasProperty(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress) {
    (void)inDriver;
    (void)inClientProcessID;

    switch (inObjectID) {
        case kAudioObjectPlugInObject:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                case kAudioObjectPropertyName:
                case kAudioObjectPropertyManufacturer:
                case kAudioObjectPropertyOwnedObjects:
                case kAudioPlugInPropertyResourceBundle:
                case kAudioPlugInPropertyDeviceList:
                case kAudioPlugInPropertyTranslateUIDToDevice:
                    return true;
                default:
                    return false;
            }

        case kObjectID_Device:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                case kAudioObjectPropertyName:
                case kAudioObjectPropertyManufacturer:
                case kAudioObjectPropertyOwnedObjects:
                case kAudioDevicePropertyDeviceUID:
                case kAudioDevicePropertyModelUID:
                case kAudioDevicePropertyTransportType:
                case kAudioDevicePropertyDeviceIsAlive:
                case kAudioDevicePropertyDeviceIsRunning:
                case kAudioDevicePropertyStreams:
                case kAudioDevicePropertyStreamConfiguration:
                case kAudioDevicePropertyNominalSampleRate:
                case kAudioDevicePropertyAvailableNominalSampleRates:
                case kAudioDevicePropertyLatency:
                case kAudioDevicePropertySafetyOffset:
                case kAudioDevicePropertyClockDomain:
                case kAudioDevicePropertyZeroTimeStampPeriod:
                case kAudioDevicePropertyPreferredChannelsForStereo:
                case kAudioDevicePropertyDeviceCanBeDefaultDevice:
                case kAudioDevicePropertyDeviceCanBeDefaultSystemDevice:
                case kAudioDevicePropertyRelatedDevices:
                case kAudioDevicePropertyIsHidden:
                case kAudioObjectPropertyControlList:
                    return true;
                default:
                    return false;
            }

        case kObjectID_Stream_Input:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                case kAudioObjectPropertyName:
                case kAudioStreamPropertyDirection:
                case kAudioStreamPropertyTerminalType:
                case kAudioStreamPropertyStartingChannel:
                case kAudioStreamPropertyLatency:
                case kAudioStreamPropertyVirtualFormat:
                case kAudioStreamPropertyPhysicalFormat:
                case kAudioStreamPropertyAvailableVirtualFormats:
                case kAudioStreamPropertyAvailablePhysicalFormats:
                case kAudioStreamPropertyIsActive:
                    return true;
                default:
                    return false;
            }

        default:
            return false;
    }
}

static OSStatus STDMETHODCALLTYPE IsPropertySettable(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, Boolean *outIsSettable) {
    (void)inDriver;
    (void)inObjectID;
    (void)inClientProcessID;
    (void)inAddress;
    if (outIsSettable == NULL) {
        return kAudioHardwareIllegalOperationError;
    }
    *outIsSettable = false;
    return 0;
}

static OSStatus STDMETHODCALLTYPE GetPropertyDataSize(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 *outDataSize) {
    (void)inDriver;
    (void)inClientProcessID;
    (void)inQualifierDataSize;
    (void)inQualifierData;
    if (outDataSize == NULL) {
        return kAudioHardwareIllegalOperationError;
    }

    switch (inObjectID) {
        case kAudioObjectPlugInObject:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyName:
                case kAudioObjectPropertyManufacturer:
                case kAudioPlugInPropertyResourceBundle:
                    *outDataSize = sizeof(CFStringRef);
                    return 0;
                case kAudioObjectPropertyOwnedObjects:
                case kAudioPlugInPropertyDeviceList:
                case kAudioPlugInPropertyTranslateUIDToDevice:
                    *outDataSize = sizeof(AudioObjectID);
                    return 0;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        case kObjectID_Device:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                case kAudioDevicePropertyTransportType:
                case kAudioDevicePropertyDeviceIsAlive:
                case kAudioDevicePropertyDeviceIsRunning:
                case kAudioDevicePropertyLatency:
                case kAudioDevicePropertySafetyOffset:
                case kAudioDevicePropertyClockDomain:
                case kAudioDevicePropertyZeroTimeStampPeriod:
                case kAudioDevicePropertyDeviceCanBeDefaultDevice:
                case kAudioDevicePropertyDeviceCanBeDefaultSystemDevice:
                case kAudioDevicePropertyIsHidden:
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioObjectPropertyName:
                case kAudioObjectPropertyManufacturer:
                case kAudioDevicePropertyDeviceUID:
                case kAudioDevicePropertyModelUID:
                    *outDataSize = sizeof(CFStringRef);
                    return 0;
                case kAudioObjectPropertyOwnedObjects:
                case kAudioDevicePropertyStreams:
                case kAudioDevicePropertyRelatedDevices:
                    *outDataSize = sizeof(AudioObjectID);
                    return 0;
                case kAudioDevicePropertyNominalSampleRate:
                    *outDataSize = sizeof(Float64);
                    return 0;
                case kAudioDevicePropertyAvailableNominalSampleRates:
                    *outDataSize = sizeof(AudioValueRange);
                    return 0;
                case kAudioDevicePropertyPreferredChannelsForStereo:
                    *outDataSize = sizeof(UInt32) * 2;
                    return 0;
                case kAudioObjectPropertyControlList:
                    *outDataSize = 0;
                    return 0;
                case kAudioDevicePropertyStreamConfiguration:
                    *outDataSize = offsetof(AudioBufferList, mBuffers) + sizeof(AudioBuffer);
                    return 0;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        case kObjectID_Stream_Input:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                case kAudioObjectPropertyClass:
                case kAudioObjectPropertyOwner:
                case kAudioStreamPropertyDirection:
                case kAudioStreamPropertyTerminalType:
                case kAudioStreamPropertyStartingChannel:
                case kAudioStreamPropertyLatency:
                case kAudioStreamPropertyIsActive:
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioObjectPropertyName:
                    *outDataSize = sizeof(CFStringRef);
                    return 0;
                case kAudioStreamPropertyVirtualFormat:
                case kAudioStreamPropertyPhysicalFormat:
                    *outDataSize = sizeof(AudioStreamBasicDescription);
                    return 0;
                case kAudioStreamPropertyAvailableVirtualFormats:
                case kAudioStreamPropertyAvailablePhysicalFormats:
                    *outDataSize = sizeof(AudioStreamRangedDescription);
                    return 0;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        default:
            return kAudioHardwareBadObjectError;
    }
}

static OSStatus STDMETHODCALLTYPE GetPropertyData(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 inDataSize, UInt32 *outDataSize, void *outData) {
    (void)inDriver;
    (void)inClientProcessID;
    if (outDataSize == NULL || outData == NULL) {
        return kAudioHardwareIllegalOperationError;
    }

    switch (inObjectID) {
        case kAudioObjectPlugInObject:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                    *((AudioClassID *)outData) = kAudioObjectClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyClass:
                    *((AudioClassID *)outData) = kAudioPlugInClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyOwner:
                    *((AudioObjectID *)outData) = kAudioObjectUnknown;
                    *outDataSize = sizeof(AudioObjectID);
                    return 0;
                case kAudioObjectPropertyName:
                    return WriteCFString(CFSTR("Vox Virtual Mic Plug-In"), inDataSize, outDataSize, outData);
                case kAudioObjectPropertyManufacturer:
                    return WriteCFString(ManufacturerName(), inDataSize, outDataSize, outData);
                case kAudioPlugInPropertyResourceBundle:
                    return WriteCFString(ResourceBundlePath(), inDataSize, outDataSize, outData);
                case kAudioObjectPropertyOwnedObjects:
                case kAudioPlugInPropertyDeviceList:
                    return WriteObjectID(kObjectID_Device, inDataSize, outDataSize, outData);
                case kAudioPlugInPropertyTranslateUIDToDevice:
                    if (inQualifierDataSize == sizeof(CFStringRef) && inQualifierData != NULL) {
                        CFStringRef requested = *((CFStringRef const *)inQualifierData);
                        AudioObjectID value = CFEqual(requested, DeviceUID()) ? kObjectID_Device : kAudioObjectUnknown;
                        return WriteObjectID(value, inDataSize, outDataSize, outData);
                    }
                    return kAudioHardwareBadPropertySizeError;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        case kObjectID_Device:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                    *((AudioClassID *)outData) = kAudioObjectClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyClass:
                    *((AudioClassID *)outData) = kAudioDeviceClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyOwner:
                    *((AudioObjectID *)outData) = kAudioObjectPlugInObject;
                    *outDataSize = sizeof(AudioObjectID);
                    return 0;
                case kAudioObjectPropertyName:
                    return WriteCFString(DeviceName(), inDataSize, outDataSize, outData);
                case kAudioObjectPropertyManufacturer:
                    return WriteCFString(ManufacturerName(), inDataSize, outDataSize, outData);
                case kAudioObjectPropertyOwnedObjects:
                case kAudioDevicePropertyStreams:
                    if (inAddress->mSelector == kAudioDevicePropertyStreams && inAddress->mScope != kAudioObjectPropertyScopeInput) {
                        *outDataSize = 0;
                        return 0;
                    }
                    return WriteObjectID(kObjectID_Stream_Input, inDataSize, outDataSize, outData);
                case kAudioDevicePropertyRelatedDevices:
                    return WriteObjectID(kObjectID_Device, inDataSize, outDataSize, outData);
                case kAudioDevicePropertyDeviceUID:
                case kAudioDevicePropertyModelUID:
                    return WriteCFString(DeviceUID(), inDataSize, outDataSize, outData);
                case kAudioDevicePropertyTransportType:
                    *((UInt32 *)outData) = kAudioDeviceTransportTypeVirtual;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyDeviceIsAlive:
                    *((UInt32 *)outData) = 1;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyDeviceIsRunning:
                    *((UInt32 *)outData) = gActiveIOCount > 0 ? 1 : 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyClockDomain:
                    *((UInt32 *)outData) = 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyZeroTimeStampPeriod:
                    *((UInt32 *)outData) = kZeroTimeStampPeriodFrames;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyDeviceCanBeDefaultDevice:
                    *((UInt32 *)outData) = 1;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyDeviceCanBeDefaultSystemDevice:
                    *((UInt32 *)outData) = 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyIsHidden:
                    *((UInt32 *)outData) = 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioDevicePropertyNominalSampleRate:
                    *((Float64 *)outData) = VMIC_DEFAULT_SAMPLE_RATE;
                    *outDataSize = sizeof(Float64);
                    return 0;
                case kAudioDevicePropertyAvailableNominalSampleRates: {
                    AudioValueRange range = {.mMinimum = VMIC_DEFAULT_SAMPLE_RATE, .mMaximum = VMIC_DEFAULT_SAMPLE_RATE};
                    if (inDataSize < sizeof(range)) {
                        return kAudioHardwareBadPropertySizeError;
                    }
                    *((AudioValueRange *)outData) = range;
                    *outDataSize = sizeof(range);
                    return 0;
                }
                case kAudioDevicePropertyPreferredChannelsForStereo: {
                    if (inDataSize < sizeof(UInt32) * 2) {
                        return kAudioHardwareBadPropertySizeError;
                    }
                    UInt32 *channels = (UInt32 *)outData;
                    channels[0] = 1;
                    channels[1] = 1;
                    *outDataSize = sizeof(UInt32) * 2;
                    return 0;
                }
                case kAudioObjectPropertyControlList:
                    *outDataSize = 0;
                    return 0;
                case kAudioDevicePropertyStreamConfiguration: {
                    UInt32 size = offsetof(AudioBufferList, mBuffers) + sizeof(AudioBuffer);
                    if (inDataSize < size) {
                        return kAudioHardwareBadPropertySizeError;
                    }
                    AudioBufferList *abl = (AudioBufferList *)outData;
                    abl->mNumberBuffers = 1;
                    abl->mBuffers[0].mNumberChannels = kVirtualMicChannelCount;
                    abl->mBuffers[0].mDataByteSize = 0;
                    abl->mBuffers[0].mData = NULL;
                    *outDataSize = size;
                    return 0;
                }
                case kAudioDevicePropertyLatency:
                case kAudioDevicePropertySafetyOffset:
                    *((UInt32 *)outData) = 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        case kObjectID_Stream_Input:
            switch (inAddress->mSelector) {
                case kAudioObjectPropertyBaseClass:
                    *((AudioClassID *)outData) = kAudioObjectClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyClass:
                    *((AudioClassID *)outData) = kAudioStreamClassID;
                    *outDataSize = sizeof(AudioClassID);
                    return 0;
                case kAudioObjectPropertyOwner:
                    *((AudioObjectID *)outData) = kObjectID_Device;
                    *outDataSize = sizeof(AudioObjectID);
                    return 0;
                case kAudioObjectPropertyName:
                    return WriteCFString(StreamName(), inDataSize, outDataSize, outData);
                case kAudioStreamPropertyDirection:
                    *((UInt32 *)outData) = 1;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioStreamPropertyTerminalType:
                    *((UInt32 *)outData) = kAudioStreamTerminalTypeMicrophone;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioStreamPropertyStartingChannel:
                    *((UInt32 *)outData) = 1;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioStreamPropertyLatency:
                    *((UInt32 *)outData) = 0;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                case kAudioStreamPropertyVirtualFormat:
                case kAudioStreamPropertyPhysicalFormat: {
                    AudioStreamBasicDescription asbd = VirtualMicFormat();
                    if (inDataSize < sizeof(asbd)) {
                        return kAudioHardwareBadPropertySizeError;
                    }
                    *((AudioStreamBasicDescription *)outData) = asbd;
                    *outDataSize = sizeof(asbd);
                    return 0;
                }
                case kAudioStreamPropertyAvailableVirtualFormats:
                case kAudioStreamPropertyAvailablePhysicalFormats: {
                    AudioStreamRangedDescription ranged = VirtualMicRangedFormat();
                    if (inDataSize < sizeof(ranged)) {
                        return kAudioHardwareBadPropertySizeError;
                    }
                    *((AudioStreamRangedDescription *)outData) = ranged;
                    *outDataSize = sizeof(ranged);
                    return 0;
                }
                case kAudioStreamPropertyIsActive:
                    *((UInt32 *)outData) = 1;
                    *outDataSize = sizeof(UInt32);
                    return 0;
                default:
                    return kAudioHardwareUnknownPropertyError;
            }

        default:
            return kAudioHardwareBadObjectError;
    }
}

static OSStatus STDMETHODCALLTYPE SetPropertyData(AudioServerPlugInDriverRef inDriver, AudioObjectID inObjectID, pid_t inClientProcessID, const AudioObjectPropertyAddress *inAddress, UInt32 inQualifierDataSize, const void *inQualifierData, UInt32 inDataSize, const void *inData) {
    (void)inDriver;
    (void)inObjectID;
    (void)inClientProcessID;
    (void)inAddress;
    (void)inQualifierDataSize;
    (void)inQualifierData;
    (void)inDataSize;
    (void)inData;
    return kAudioHardwareUnsupportedOperationError;
}

static OSStatus STDMETHODCALLTYPE StartIO(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID) {
    (void)inDriver;
    (void)inClientID;
    if (inDeviceObjectID != kObjectID_Device) {
        return kAudioHardwareBadObjectError;
    }

    if (gActiveIOCount == 0) {
        gStartHostTime = mach_absolute_time();
        gAnchorHostTime = gStartHostTime;
        gPeriodCounter = 0;
        UpdateClockCalibration();
        gClockSeed += 1;
        OSStatus status = EnsureSocketReady();
        if (status != 0) {
            return status;
        }
    }
    gActiveIOCount += 1;
    return 0;
}

static OSStatus STDMETHODCALLTYPE StopIO(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID) {
    (void)inDriver;
    (void)inClientID;
    if (inDeviceObjectID != kObjectID_Device) {
        return kAudioHardwareBadObjectError;
    }

    if (gActiveIOCount > 0) {
        gActiveIOCount -= 1;
    }
    if (gActiveIOCount == 0) {
        CloseSocket();
        ResetRing();
    }
    return 0;
}

static OSStatus STDMETHODCALLTYPE GetZeroTimeStamp(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, Float64 *outSampleTime, UInt64 *outHostTime, UInt64 *outSeed) {
    (void)inDriver;
    (void)inClientID;
    if (inDeviceObjectID != kObjectID_Device || outSampleTime == NULL || outHostTime == NULL || outSeed == NULL) {
        return kAudioHardwareIllegalOperationError;
    }

    if (gHostTicksPerFrame <= 0) {
        UpdateClockCalibration();
    }

    const UInt64 currentHostTime = mach_absolute_time();
    const Float64 hostTicksPerPeriod = gHostTicksPerFrame * (Float64)kZeroTimeStampPeriodFrames;
    const UInt64 nextPeriodHostTime = gAnchorHostTime + (UInt64)((Float64)(gPeriodCounter + 1) * hostTicksPerPeriod);
    if (currentHostTime >= nextPeriodHostTime) {
        gPeriodCounter += 1;
    }

    *outSampleTime = (Float64)gPeriodCounter * (Float64)kZeroTimeStampPeriodFrames;
    *outHostTime = gAnchorHostTime + (UInt64)((Float64)gPeriodCounter * hostTicksPerPeriod);
    *outSeed = gClockSeed;
    return 0;
}

static OSStatus STDMETHODCALLTYPE WillDoIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, Boolean *outWillDo, Boolean *outWillDoInPlace) {
    (void)inDriver;
    (void)inClientID;
    if (inDeviceObjectID != kObjectID_Device || outWillDo == NULL || outWillDoInPlace == NULL) {
        return kAudioHardwareIllegalOperationError;
    }
    *outWillDo = (inOperationID == kAudioServerPlugInIOOperationReadInput);
    *outWillDoInPlace = true;
    return 0;
}

static OSStatus STDMETHODCALLTYPE BeginIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo) {
    (void)inDriver;
    (void)inDeviceObjectID;
    (void)inClientID;
    (void)inOperationID;
    (void)inIOBufferFrameSize;
    (void)inIOCycleInfo;
    return 0;
}

static OSStatus STDMETHODCALLTYPE DoIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, AudioObjectID inStreamObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo, void *ioMainBuffer, void *ioSecondaryBuffer) {
    (void)inDriver;
    (void)inClientID;
    (void)inIOCycleInfo;
    (void)ioSecondaryBuffer;

    if (inDeviceObjectID != kObjectID_Device || inStreamObjectID != kObjectID_Stream_Input) {
        return kAudioHardwareBadObjectError;
    }
    if (inOperationID != kAudioServerPlugInIOOperationReadInput || ioMainBuffer == NULL) {
        return 0;
    }

    PumpUDP();
    Float32 *buffer = (Float32 *)ioMainBuffer;
    ReadFrames(buffer, inIOBufferFrameSize);
    return 0;
}

static OSStatus STDMETHODCALLTYPE EndIOOperation(AudioServerPlugInDriverRef inDriver, AudioObjectID inDeviceObjectID, UInt32 inClientID, UInt32 inOperationID, UInt32 inIOBufferFrameSize, const AudioServerPlugInIOCycleInfo *inIOCycleInfo) {
    (void)inDriver;
    (void)inDeviceObjectID;
    (void)inClientID;
    (void)inOperationID;
    (void)inIOBufferFrameSize;
    (void)inIOCycleInfo;
    return 0;
}
