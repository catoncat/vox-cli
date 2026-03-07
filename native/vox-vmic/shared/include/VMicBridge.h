#ifndef VMIC_BRIDGE_H
#define VMIC_BRIDGE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VMIC_BRIDGE_VERSION 1U
#define VMIC_DEFAULT_SAMPLE_RATE 48000.0
#define VMIC_DEFAULT_CHANNELS 1U
#define VMIC_DEFAULT_CAPACITY_FRAMES (uint64_t)(VMIC_DEFAULT_SAMPLE_RATE * 30.0)

enum {
    VMIC_STATE_IDLE = 0,
    VMIC_STATE_READY = 1,
    VMIC_STATE_DRAINING = 2,
};

typedef struct VMicSnapshot {
    uint32_t version;
    uint32_t channels;
    double sampleRate;
    uint64_t capacityFrames;
    uint64_t writeIndex;
    uint64_t readIndex;
    uint64_t queuedFrames;
    uint32_t state;
    uint32_t generation;
} VMicSnapshot;

typedef struct VMicWriter VMicWriter;
typedef struct VMicReader VMicReader;

int vmic_default_shared_path(char *buffer, size_t size);
const char *vmic_state_name(uint32_t state);

VMicWriter *vmic_writer_open(const char *path,
                             uint64_t capacityFrames,
                             uint32_t channels,
                             double sampleRate,
                             int *outErrno);
int vmic_writer_reset(VMicWriter *writer);
int vmic_writer_enqueue(VMicWriter *writer, const float *interleavedSamples, uint64_t frameCount);
int vmic_writer_snapshot(VMicWriter *writer, VMicSnapshot *snapshot);
void vmic_writer_close(VMicWriter *writer);

VMicReader *vmic_reader_open(const char *path, int *outErrno);
int vmic_reader_snapshot(VMicReader *reader, VMicSnapshot *snapshot);
uint64_t vmic_reader_dequeue(VMicReader *reader, float *interleavedOutput, uint64_t requestedFrames);
void vmic_reader_close(VMicReader *reader);

#ifdef __cplusplus
}
#endif

#endif
