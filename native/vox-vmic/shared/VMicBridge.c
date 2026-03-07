#include "VMicBridge.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define VMIC_MAGIC 0x564D4943U

typedef struct VMicSharedHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t channels;
    uint32_t reserved0;
    double sampleRate;
    uint64_t capacityFrames;
    _Atomic uint64_t writeIndex;
    _Atomic uint64_t readIndex;
    _Atomic uint32_t state;
    _Atomic uint32_t generation;
} VMicSharedHeader;

struct VMicWriter {
    int fd;
    size_t mappedSize;
    void *mapping;
};

struct VMicReader {
    int fd;
    size_t mappedSize;
    void *mapping;
};

static size_t vmic_header_size(void) {
    return sizeof(VMicSharedHeader);
}

static size_t vmic_total_bytes(uint64_t capacityFrames, uint32_t channels) {
    return vmic_header_size() + (size_t)(capacityFrames * channels * sizeof(float));
}

static VMicSharedHeader *vmic_header(void *mapping) {
    return (VMicSharedHeader *)mapping;
}

static float *vmic_frames(void *mapping) {
    return (float *)((uint8_t *)mapping + vmic_header_size());
}

static int ensure_dir(const char *path) {
    struct stat st;
    if (stat(path, &st) == 0) {
        return S_ISDIR(st.st_mode) ? 0 : ENOTDIR;
    }
    if (mkdir(path, 0700) == 0 || errno == EEXIST) {
        return 0;
    }
    return errno;
}

int vmic_default_shared_path(char *buffer, size_t size) {
    if (buffer == NULL || size == 0) {
        return EINVAL;
    }

    size_t baseSize = confstr(_CS_DARWIN_USER_TEMP_DIR, NULL, 0);
    if (baseSize == 0) {
        return errno ? errno : ENOENT;
    }

    char *base = calloc(baseSize + 1, 1);
    if (base == NULL) {
        return ENOMEM;
    }

    if (confstr(_CS_DARWIN_USER_TEMP_DIR, base, baseSize) == 0) {
        int err = errno ? errno : ENOENT;
        free(base);
        return err;
    }

    char dirPath[PATH_MAX];
    int written = snprintf(dirPath, sizeof(dirPath), "%s%s", base, "com.envvar.vox.vmic");
    free(base);
    if (written < 0 || (size_t)written >= sizeof(dirPath)) {
        return ENAMETOOLONG;
    }

    int dirErr = ensure_dir(dirPath);
    if (dirErr != 0) {
        return dirErr;
    }

    written = snprintf(buffer, size, "%s/%s", dirPath, "stream.bin");
    if (written < 0 || (size_t)written >= size) {
        return ENAMETOOLONG;
    }
    return 0;
}

static int resolve_path(const char *path, char *buffer, size_t size) {
    if (path != NULL && path[0] != '\0') {
        int written = snprintf(buffer, size, "%s", path);
        return (written < 0 || (size_t)written >= size) ? ENAMETOOLONG : 0;
    }
    return vmic_default_shared_path(buffer, size);
}

static int map_file(const char *path, size_t size, int createIfMissing, int *fdOut, void **mappingOut) {
    int flags = O_RDWR;
    if (createIfMissing) {
        flags |= O_CREAT;
    }

    int fd = open(path, flags, 0600);
    if (fd < 0) {
        return errno;
    }

    if (createIfMissing && ftruncate(fd, (off_t)size) != 0) {
        int err = errno;
        close(fd);
        return err;
    }

    struct stat st;
    if (fstat(fd, &st) != 0) {
        int err = errno;
        close(fd);
        return err;
    }

    if ((size_t)st.st_size < size) {
        size = (size_t)st.st_size;
    }

    void *mapping = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) {
        int err = errno;
        close(fd);
        return err;
    }

    *fdOut = fd;
    *mappingOut = mapping;
    return 0;
}

static void close_mapping(int fd, void *mapping, size_t size) {
    if (mapping != NULL && mapping != MAP_FAILED) {
        munmap(mapping, size);
    }
    if (fd >= 0) {
        close(fd);
    }
}

static void init_header(VMicSharedHeader *header, uint64_t capacityFrames, uint32_t channels, double sampleRate) {
    header->magic = VMIC_MAGIC;
    header->version = VMIC_BRIDGE_VERSION;
    header->channels = channels;
    header->reserved0 = 0;
    header->sampleRate = sampleRate;
    header->capacityFrames = capacityFrames;
    atomic_store(&header->writeIndex, 0);
    atomic_store(&header->readIndex, 0);
    atomic_store(&header->state, VMIC_STATE_IDLE);
    atomic_store(&header->generation, 1);
}

static bool header_matches(const VMicSharedHeader *header, uint64_t capacityFrames, uint32_t channels, double sampleRate) {
    return header->magic == VMIC_MAGIC &&
           header->version == VMIC_BRIDGE_VERSION &&
           header->capacityFrames == capacityFrames &&
           header->channels == channels &&
           header->sampleRate == sampleRate;
}

VMicWriter *vmic_writer_open(const char *path, uint64_t capacityFrames, uint32_t channels, double sampleRate, int *outErrno) {
    if (capacityFrames == 0 || channels == 0) {
        if (outErrno) *outErrno = EINVAL;
        return NULL;
    }

    char resolvedPath[PATH_MAX];
    int pathErr = resolve_path(path, resolvedPath, sizeof(resolvedPath));
    if (pathErr != 0) {
        if (outErrno) *outErrno = pathErr;
        return NULL;
    }

    size_t mappedSize = vmic_total_bytes(capacityFrames, channels);
    int fd = -1;
    void *mapping = NULL;
    int mapErr = map_file(resolvedPath, mappedSize, 1, &fd, &mapping);
    if (mapErr != 0) {
        if (outErrno) *outErrno = mapErr;
        return NULL;
    }

    VMicSharedHeader *header = vmic_header(mapping);
    if (!header_matches(header, capacityFrames, channels, sampleRate)) {
        memset(mapping, 0, mappedSize);
        init_header(header, capacityFrames, channels, sampleRate);
    }

    VMicWriter *writer = calloc(1, sizeof(VMicWriter));
    if (writer == NULL) {
        close_mapping(fd, mapping, mappedSize);
        if (outErrno) *outErrno = ENOMEM;
        return NULL;
    }

    writer->fd = fd;
    writer->mappedSize = mappedSize;
    writer->mapping = mapping;
    if (outErrno) *outErrno = 0;
    return writer;
}

static uint64_t queued_frames(const VMicSharedHeader *header) {
    uint64_t writeIndex = atomic_load(&header->writeIndex);
    uint64_t readIndex = atomic_load(&header->readIndex);
    return writeIndex >= readIndex ? (writeIndex - readIndex) : 0;
}

int vmic_writer_reset(VMicWriter *writer) {
    if (writer == NULL || writer->mapping == NULL) {
        return EINVAL;
    }
    VMicSharedHeader *header = vmic_header(writer->mapping);
    atomic_store(&header->writeIndex, 0);
    atomic_store(&header->readIndex, 0);
    atomic_store(&header->state, VMIC_STATE_IDLE);
    atomic_fetch_add(&header->generation, 1);
    return 0;
}

int vmic_writer_enqueue(VMicWriter *writer, const float *interleavedSamples, uint64_t frameCount) {
    if (writer == NULL || writer->mapping == NULL || interleavedSamples == NULL) {
        return EINVAL;
    }

    VMicSharedHeader *header = vmic_header(writer->mapping);
    const uint32_t channels = header->channels;
    const uint64_t capacityFrames = header->capacityFrames;
    const uint64_t pending = queued_frames(header);
    const uint64_t freeFrames = capacityFrames > pending ? (capacityFrames - pending) : 0;
    if (frameCount > freeFrames) {
        return ENOSPC;
    }

    float *frames = vmic_frames(writer->mapping);
    uint64_t writeIndex = atomic_load(&header->writeIndex);
    for (uint64_t frame = 0; frame < frameCount; ++frame) {
        uint64_t slot = (writeIndex + frame) % capacityFrames;
        memcpy(frames + (slot * channels), interleavedSamples + (frame * channels), sizeof(float) * channels);
    }

    atomic_store(&header->writeIndex, writeIndex + frameCount);
    atomic_store(&header->state, VMIC_STATE_READY);
    return 0;
}

static void fill_snapshot(const VMicSharedHeader *header, VMicSnapshot *snapshot) {
    snapshot->version = header->version;
    snapshot->channels = header->channels;
    snapshot->sampleRate = header->sampleRate;
    snapshot->capacityFrames = header->capacityFrames;
    snapshot->writeIndex = atomic_load(&header->writeIndex);
    snapshot->readIndex = atomic_load(&header->readIndex);
    snapshot->queuedFrames = snapshot->writeIndex >= snapshot->readIndex ? (snapshot->writeIndex - snapshot->readIndex) : 0;
    snapshot->state = atomic_load(&header->state);
    snapshot->generation = atomic_load(&header->generation);
}

int vmic_writer_snapshot(VMicWriter *writer, VMicSnapshot *snapshot) {
    if (writer == NULL || writer->mapping == NULL || snapshot == NULL) {
        return EINVAL;
    }
    fill_snapshot(vmic_header(writer->mapping), snapshot);
    return 0;
}

void vmic_writer_close(VMicWriter *writer) {
    if (writer == NULL) {
        return;
    }
    close_mapping(writer->fd, writer->mapping, writer->mappedSize);
    free(writer);
}

VMicReader *vmic_reader_open(const char *path, int *outErrno) {
    char resolvedPath[PATH_MAX];
    int pathErr = resolve_path(path, resolvedPath, sizeof(resolvedPath));
    if (pathErr != 0) {
        if (outErrno) *outErrno = pathErr;
        return NULL;
    }

    struct stat st;
    if (stat(resolvedPath, &st) != 0) {
        if (outErrno) *outErrno = errno;
        return NULL;
    }

    int fd = -1;
    void *mapping = NULL;
    int mapErr = map_file(resolvedPath, (size_t)st.st_size, 0, &fd, &mapping);
    if (mapErr != 0) {
        if (outErrno) *outErrno = mapErr;
        return NULL;
    }

    VMicSharedHeader *header = vmic_header(mapping);
    if (header->magic != VMIC_MAGIC || header->version != VMIC_BRIDGE_VERSION) {
        close_mapping(fd, mapping, (size_t)st.st_size);
        if (outErrno) *outErrno = EPROTO;
        return NULL;
    }

    VMicReader *reader = calloc(1, sizeof(VMicReader));
    if (reader == NULL) {
        close_mapping(fd, mapping, (size_t)st.st_size);
        if (outErrno) *outErrno = ENOMEM;
        return NULL;
    }

    reader->fd = fd;
    reader->mappedSize = (size_t)st.st_size;
    reader->mapping = mapping;
    if (outErrno) *outErrno = 0;
    return reader;
}

int vmic_reader_snapshot(VMicReader *reader, VMicSnapshot *snapshot) {
    if (reader == NULL || reader->mapping == NULL || snapshot == NULL) {
        return EINVAL;
    }
    fill_snapshot(vmic_header(reader->mapping), snapshot);
    return 0;
}

uint64_t vmic_reader_dequeue(VMicReader *reader, float *interleavedOutput, uint64_t requestedFrames) {
    if (reader == NULL || reader->mapping == NULL || interleavedOutput == NULL || requestedFrames == 0) {
        return 0;
    }

    VMicSharedHeader *header = vmic_header(reader->mapping);
    const uint32_t channels = header->channels;
    const uint64_t capacityFrames = header->capacityFrames;
    const uint64_t available = queued_frames(header);
    const uint64_t toRead = requestedFrames < available ? requestedFrames : available;

    float *frames = vmic_frames(reader->mapping);
    uint64_t readIndex = atomic_load(&header->readIndex);
    for (uint64_t frame = 0; frame < toRead; ++frame) {
        uint64_t slot = (readIndex + frame) % capacityFrames;
        memcpy(interleavedOutput + (frame * channels), frames + (slot * channels), sizeof(float) * channels);
    }

    if (toRead > 0) {
        atomic_store(&header->readIndex, readIndex + toRead);
    }

    const uint64_t remaining = available > toRead ? (available - toRead) : 0;
    atomic_store(&header->state, remaining == 0 ? VMIC_STATE_IDLE : VMIC_STATE_DRAINING);
    return toRead;
}

void vmic_reader_close(VMicReader *reader) {
    if (reader == NULL) {
        return;
    }
    close_mapping(reader->fd, reader->mapping, reader->mappedSize);
    free(reader);
}

const char *vmic_state_name(uint32_t state) {
    switch (state) {
        case VMIC_STATE_IDLE:
            return "idle";
        case VMIC_STATE_READY:
            return "ready";
        case VMIC_STATE_DRAINING:
            return "draining";
        default:
            return "unknown";
    }
}
